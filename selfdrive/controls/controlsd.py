#!/usr/bin/env python3
import os
import math
import time
import threading
from types import SimpleNamespace
from typing import SupportsFloat

import cereal.messaging as messaging
import openpilot.selfdrive.sentry as sentry

from cereal import car, custom, log
from cereal.visionipc import VisionIpcClient, VisionStreamType


from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import clip
from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process, Priority, Ratekeeper, DT_CTRL
from openpilot.common.swaglog import cloudlog

from openpilot.selfdrive.car.car_helpers import get_startup_event
from openpilot.selfdrive.car.card import CarD
from openpilot.selfdrive.controls.lib.alertmanager import AlertManager, set_offroad_alert
from openpilot.selfdrive.controls.lib.drive_helpers import VCruiseHelper, clip_curvature
from openpilot.selfdrive.controls.lib.events import Events, ET
from openpilot.selfdrive.controls.lib.latcontrol import LatControl, MIN_LATERAL_CONTROL_SPEED
from openpilot.selfdrive.controls.lib.latcontrol_pid import LatControlPID
from openpilot.selfdrive.controls.lib.latcontrol_angle import LatControlAngle, STEER_ANGLE_SATURATION_THRESHOLD
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.selfdrive.controls.lib.longcontrol import LongControl
from openpilot.selfdrive.controls.lib.vehicle_model import VehicleModel

from openpilot.system.hardware import HARDWARE
from openpilot.system.version import get_short_branch

from openpilot.selfdrive.frogpilot.controls.lib.frogpilot_functions import CRUISING_SPEED, PROBABILITY, MovingAverageCalculator

from openpilot.selfdrive.frogpilot.controls.lib.model_manager import RADARLESS_MODELS
from openpilot.selfdrive.frogpilot.controls.lib.speed_limit_controller import SpeedLimitController

SOFT_DISABLE_TIME = 3  # seconds
LDW_MIN_SPEED = 31 * CV.MPH_TO_MS
LANE_DEPARTURE_THRESHOLD = 0.1
CAMERA_OFFSET = 0.04

REPLAY = "REPLAY" in os.environ
SIMULATION = "SIMULATION" in os.environ
TESTING_CLOSET = "TESTING_CLOSET" in os.environ
IGNORE_PROCESSES = {"loggerd", "encoderd", "statsd"}

ThermalStatus = log.DeviceState.ThermalStatus
State = log.ControlsState.OpenpilotState
PandaType = log.PandaState.PandaType
Desire = log.Desire
LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection
EventName = car.CarEvent.EventName
ButtonType = car.CarState.ButtonEvent.Type
GearShifter = car.CarState.GearShifter
SafetyModel = car.CarParams.SafetyModel
FrogPilotButtonType = custom.FrogPilotCarState.ButtonEvent.Type

IGNORED_SAFETY_MODES = (SafetyModel.silent, SafetyModel.noOutput)
CSID_MAP = {"1": EventName.roadCameraError, "2": EventName.wideRoadCameraError, "0": EventName.driverCameraError}
ACTUATOR_FIELDS = tuple(car.CarControl.Actuators.schema.fields.keys())
ACTIVE_STATES = (State.enabled, State.softDisabling, State.overriding)
ENABLED_STATES = (State.preEnabled, *ACTIVE_STATES)


class Controls:
  def __init__(self, CI=None):
    self.card = CarD(CI)

    self.params = Params()
    self.params_memory = Params("/dev/shm/params")
    self.params_storage = Params("/persist/params")

    self.radarless_model = self.params.get("Model", block=True, encoding='utf-8') in RADARLESS_MODELS

    with car.CarParams.from_bytes(self.params.get("CarParams", block=True)) as msg:
      # TODO: this shouldn't need to be a builder
      self.CP = msg.as_builder()

    self.CI = self.card.CI


    # Ensure the current branch is cached, otherwise the first iteration of controlsd lags
    self.branch = get_short_branch()

    # Setup sockets
    self.pm = messaging.PubMaster(['controlsState', 'carControl', 'onroadEvents', 'frogpilotCarControl'])

    self.sensor_packets = ["accelerometer", "gyroscope"]
    self.camera_packets = ["roadCameraState", "driverCameraState", "wideRoadCameraState"]

    self.log_sock = messaging.sub_sock('androidLog')

    ignore = self.sensor_packets + ['testJoystick']
    if SIMULATION:
      ignore += ['driverCameraState', 'managerState']
    if self.radarless_model:
      ignore += ['radarState']
    self.sm = messaging.SubMaster(['deviceState', 'pandaStates', 'peripheralState', 'modelV2', 'liveCalibration',
                                   'carOutput', 'driverMonitoringState', 'longitudinalPlan', 'liveLocationKalman',
                                   'managerState', 'liveParameters', 'radarState', 'liveTorqueParameters',
                                   'testJoystick', 'frogpilotPlan'] + self.camera_packets + self.sensor_packets,
                                  ignore_alive=ignore, ignore_avg_freq=ignore+['radarState', 'testJoystick'], ignore_valid=['testJoystick', ],
                                  frequency=int(1/DT_CTRL))

    self.joystick_mode = self.params.get_bool("JoystickDebugMode")

    # read params
    self.disengage_on_accelerator = self.params.get_bool("DisengageOnAccelerator")
    self.is_metric = self.params.get_bool("IsMetric")
    self.is_ldw_enabled = self.params.get_bool("IsLdwEnabled")

    # detect sound card presence and ensure successful init
    sounds_available = HARDWARE.get_sound_card_online()

    car_recognized = self.CP.carName != 'mock'

    # cleanup old params
    if not self.CP.experimentalLongitudinalAvailable:
      self.params.remove("ExperimentalLongitudinalEnabled")
    if not self.CP.openpilotLongitudinalControl:
      self.params.remove("ExperimentalMode")

    self.CC = car.CarControl.new_message()
    self.CS_prev = car.CarState.new_message()
    self.FPCC = custom.FrogPilotCarControl.new_message()
    self.AM = AlertManager()
    self.events = Events()

    self.LoC = LongControl(self.CP)
    self.VM = VehicleModel(self.CP)

    self.LaC: LatControl
    if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
      self.LaC = LatControlAngle(self.CP, self.CI)
    elif self.CP.lateralTuning.which() == 'pid':
      self.LaC = LatControlPID(self.CP, self.CI)
    elif self.CP.lateralTuning.which() == 'torque':
      self.LaC = LatControlTorque(self.CP, self.CI)

    self.initialized = False
    self.state = State.disabled
    self.enabled = False
    self.active = False
    self.soft_disable_timer = 0
    self.mismatch_counter = 0
    self.cruise_mismatch_counter = 0
    self.last_blinker_frame = 0
    self.last_steering_pressed_frame = 0
    self.distance_traveled = 0
    self.last_functional_fan_frame = 0
    self.events_prev = []
    self.current_alert_types = [ET.PERMANENT]
    self.logged_comm_issue = None
    self.not_running_prev = None
    self.steer_limited = False
    self.desired_curvature = 0.0
    self.experimental_mode = False
    self.personality = self.read_personality_param()
    self.v_cruise_helper = VCruiseHelper(self.CP)
    self.recalibrating_seen = False

    self.can_log_mono_time = 0

    self.startup_event = get_startup_event(car_recognized, not self.CP.passive, len(self.CP.carFw) > 0)

    if not sounds_available:
      self.events.add(EventName.soundsUnavailable, static=True)
    if not car_recognized:
      self.events.add(EventName.carUnrecognized, static=True)
      if len(self.CP.carFw) > 0:
        set_offroad_alert("Offroad_CarUnrecognized", True)
      else:
        set_offroad_alert("Offroad_NoFirmware", True)
    elif self.CP.passive:
      self.events.add(EventName.dashcamMode, static=True)

    # controlsd is driven by can recv, expected at 100Hz
    self.rk = Ratekeeper(100, print_delay_threshold=None)

    # FrogPilot variables
    self.frogpilot_variables = SimpleNamespace()

    self.block_user = self.branch == "FrogPilot-Development" and not self.params_storage.get_bool("FrogsGoMoo")

    self.always_on_lateral = self.params.get_bool("AlwaysOnLateral")
    self.always_on_lateral_main = self.always_on_lateral and self.params.get_bool("AlwaysOnLateralMain")

    self.drive_added = False
    self.fcw_random_event_triggered = False
    self.holiday_theme_alerted = False
    self.onroad_distance_pressed = False
    self.openpilot_crashed_triggered = False
    self.previously_enabled = False
    self.random_event_triggered = False
    self.speed_check = False

    self.drive_distance = 0
    self.drive_time = 0
    self.max_acceleration = 0
    self.previous_lead_distance = 0
    self.previous_speed_limit = 0
    self.random_event_timer = 0
    self.speed_limit_timer = 0

    self.green_light_mac = MovingAverageCalculator()

    self.update_frogpilot_params()

  def set_initial_state(self):
    if REPLAY:
      controls_state = Params().get("ReplayControlsState")
      if controls_state is not None:
        with log.ControlsState.from_bytes(controls_state) as controls_state:
          self.v_cruise_helper.v_cruise_kph = controls_state.vCruise

      if any(ps.controlsAllowed for ps in self.sm['pandaStates']):
        self.state = State.enabled

  def update_events(self, CS):
    """Compute onroadEvents from carState"""

    self.events.clear()

    # Add joystick event, static on cars, dynamic on nonCars
    if self.joystick_mode:
      self.events.add(EventName.joystickDebug)
      self.startup_event = None

    # Add startup event
    if self.startup_event is not None:
      self.events.add(self.startup_event)
      self.startup_event = None

    # Don't add any more events if not initialized
    if not self.initialized:
      self.events.add(EventName.controlsInitializing)
      return

    # no more events while in dashcam mode
    if self.CP.passive:
      return

    # Block resume if cruise never previously enabled
    resume_pressed = any(be.type in (ButtonType.accelCruise, ButtonType.resumeCruise) for be in CS.buttonEvents)
    if not self.CP.pcmCruise and not self.v_cruise_helper.v_cruise_initialized and resume_pressed:
      self.events.add(EventName.resumeBlocked)

    # Disable on rising edge of accelerator or brake. Also disable on brake when speed > 0
    if (CS.gasPressed and not self.CS_prev.gasPressed and self.disengage_on_accelerator) or \
      (CS.brakePressed and (not self.CS_prev.brakePressed or not CS.standstill)) or \
      (CS.regenBraking and (not self.CS_prev.regenBraking or not CS.standstill)):
      self.events.add(EventName.pedalPressed)

    if CS.brakePressed and CS.standstill:
      self.events.add(EventName.preEnableStandstill)

    if CS.gasPressed:
      self.events.add(EventName.gasPressedOverride)

    if not self.CP.notCar:
      self.events.add_from_msg(self.sm['driverMonitoringState'].events)

    # Add car events, ignore if CAN isn't valid
    if CS.canValid:
      self.events.add_from_msg(CS.events)

    # Create events for temperature, disk space, and memory
    if self.sm['deviceState'].thermalStatus >= ThermalStatus.red:
      if not self.increase_thermal_limits or self.sm['deviceState'].thermalStatus == ThermalStatus.danger:
        self.events.add(EventName.overheat)
    if self.sm['deviceState'].freeSpacePercent < 7 and not SIMULATION:
      # under 7% of space free no enable allowed
      self.events.add(EventName.outOfSpace)
    if self.sm['deviceState'].memoryUsagePercent > 90 and not SIMULATION:
      self.events.add(EventName.lowMemory)

    # TODO: enable this once loggerd CPU usage is more reasonable
    #cpus = list(self.sm['deviceState'].cpuUsagePercent)
    #if max(cpus, default=0) > 95 and not SIMULATION:
    #  self.events.add(EventName.highCpuUsage)

    # Alert if fan isn't spinning for 5 seconds
    if self.sm['peripheralState'].pandaType != log.PandaState.PandaType.unknown:
      if self.sm['peripheralState'].fanSpeedRpm < 500 and self.sm['deviceState'].fanSpeedPercentDesired > 50:
        # allow enough time for the fan controller in the panda to recover from stalls
        if (self.sm.frame - self.last_functional_fan_frame) * DT_CTRL > 15.0:
          self.events.add(EventName.fanMalfunction)
      else:
        self.last_functional_fan_frame = self.sm.frame

    # Handle calibration status
    cal_status = self.sm['liveCalibration'].calStatus
    if cal_status != log.LiveCalibrationData.Status.calibrated:
      if cal_status == log.LiveCalibrationData.Status.uncalibrated:
        self.events.add(EventName.calibrationIncomplete)
      elif cal_status == log.LiveCalibrationData.Status.recalibrating:
        if not self.recalibrating_seen:
          set_offroad_alert("Offroad_Recalibration", True)
        self.recalibrating_seen = True
        self.events.add(EventName.calibrationRecalibrating)
      else:
        self.events.add(EventName.calibrationInvalid)

    # Handle lane change
    if self.sm['modelV2'].meta.laneChangeState == LaneChangeState.preLaneChange:
      direction = self.sm['modelV2'].meta.laneChangeDirection
      if (CS.leftBlindspot and direction == LaneChangeDirection.left) or \
         (CS.rightBlindspot and direction == LaneChangeDirection.right):
        if self.loud_blindspot_alert:
          self.events.add(EventName.laneChangeBlockedLoud)
        else:
          self.events.add(EventName.laneChangeBlocked)
      else:
        if direction == LaneChangeDirection.left:
          if self.sm['frogpilotPlan'].laneWidthLeft >= self.lane_detection_width:
            self.events.add(EventName.preLaneChangeLeft)
          else:
            self.events.add(EventName.noLaneAvailable)
        else:
          if self.sm['frogpilotPlan'].laneWidthRight >= self.lane_detection_width:
            self.events.add(EventName.preLaneChangeRight)
          else:
            self.events.add(EventName.noLaneAvailable)
    elif self.sm['modelV2'].meta.laneChangeState in (LaneChangeState.laneChangeStarting,
                                                    LaneChangeState.laneChangeFinishing):
      self.events.add(EventName.laneChange)

    for i, pandaState in enumerate(self.sm['pandaStates']):
      # All pandas must match the list of safetyConfigs, and if outside this list, must be silent or noOutput
      if i < len(self.CP.safetyConfigs):
        safety_mismatch = pandaState.safetyModel != self.CP.safetyConfigs[i].safetyModel or \
                          pandaState.safetyParam != self.CP.safetyConfigs[i].safetyParam or \
                          pandaState.alternativeExperience != self.CP.alternativeExperience
      else:
        safety_mismatch = pandaState.safetyModel not in IGNORED_SAFETY_MODES

      # safety mismatch allows some time for boardd to set the safety mode and publish it back from panda
      if (safety_mismatch and self.sm.frame*DT_CTRL > 10.) or pandaState.safetyRxChecksInvalid or self.mismatch_counter >= 200:
        self.events.add(EventName.controlsMismatch)

      if log.PandaState.FaultType.relayMalfunction in pandaState.faults:
        self.events.add(EventName.relayMalfunction)

    # Handle HW and system malfunctions
    # Order is very intentional here. Be careful when modifying this.
    # All events here should at least have NO_ENTRY and SOFT_DISABLE.
    num_events = len(self.events)

    not_running = {p.name for p in self.sm['managerState'].processes if not p.running and p.shouldBeRunning}
    if self.sm.recv_frame['managerState'] and (not_running - IGNORE_PROCESSES):
      self.events.add(EventName.processNotRunning)
      if not_running != self.not_running_prev:
        cloudlog.event("process_not_running", not_running=not_running, error=True)
      self.not_running_prev = not_running
    else:
      if not SIMULATION and not self.rk.lagging:
        if not self.sm.all_alive(self.camera_packets):
          self.events.add(EventName.cameraMalfunction)
        elif not self.sm.all_freq_ok(self.camera_packets):
          self.events.add(EventName.cameraFrameRate)
    if not REPLAY and self.rk.lagging:
      self.events.add(EventName.controlsdLagging)
    if not self.radarless_model:
      if len(self.sm['radarState'].radarErrors) or (not self.rk.lagging and not self.sm.all_checks(['radarState'])):
        self.events.add(EventName.radarFault)
    if not self.sm.valid['pandaStates']:
      self.events.add(EventName.usbError)
    if CS.canTimeout:
      self.events.add(EventName.canBusMissing)
    elif not CS.canValid:
      self.events.add(EventName.canError)

    # generic catch-all. ideally, a more specific event should be added above instead
    has_disable_events = self.events.contains(ET.NO_ENTRY) and (self.events.contains(ET.SOFT_DISABLE) or self.events.contains(ET.IMMEDIATE_DISABLE))
    no_system_errors = (not has_disable_events) or (len(self.events) == num_events)
    if (not self.sm.all_checks() or self.card.can_rcv_timeout) and no_system_errors:
      if not self.sm.all_alive():
        self.events.add(EventName.commIssue)
      elif not self.sm.all_freq_ok():
        self.events.add(EventName.commIssueAvgFreq)
      else:  # invalid or can_rcv_timeout.
        self.events.add(EventName.commIssue)

      logs = {
        'invalid': [s for s, valid in self.sm.valid.items() if not valid],
        'not_alive': [s for s, alive in self.sm.alive.items() if not alive],
        'not_freq_ok': [s for s, freq_ok in self.sm.freq_ok.items() if not freq_ok],
        'can_rcv_timeout': self.card.can_rcv_timeout,
      }
      if logs != self.logged_comm_issue:
        cloudlog.event("commIssue", error=True, **logs)
        self.logged_comm_issue = logs
    else:
      self.logged_comm_issue = None

    if not (self.CP.notCar and self.joystick_mode):
      if not self.sm['liveLocationKalman'].posenetOK:
        self.events.add(EventName.posenetInvalid)
      if not self.sm['liveLocationKalman'].deviceStable:
        self.events.add(EventName.deviceFalling)
      if not self.sm['liveLocationKalman'].inputsOK:
        self.events.add(EventName.locationdTemporaryError)
      if not self.sm['liveParameters'].valid and not TESTING_CLOSET and (not SIMULATION or REPLAY):
        self.events.add(EventName.paramsdTemporaryError)

    # conservative HW alert. if the data or frequency are off, locationd will throw an error
    if any((self.sm.frame - self.sm.recv_frame[s])*DT_CTRL > 10. for s in self.sensor_packets):
      self.events.add(EventName.sensorDataInvalid)

    if not REPLAY:
      # Check for mismatch between openpilot and car's PCM
      cruise_mismatch = CS.cruiseState.enabled and (not self.enabled or not self.CP.pcmCruise)
      self.cruise_mismatch_counter = self.cruise_mismatch_counter + 1 if cruise_mismatch else 0
      if self.cruise_mismatch_counter > int(6. / DT_CTRL):
        self.events.add(EventName.cruiseMismatch)

    # Check for FCW
    stock_long_is_braking = self.enabled and not self.CP.openpilotLongitudinalControl and CS.aEgo < -1.25
    model_fcw = self.sm['modelV2'].meta.hardBrakePredicted and not CS.brakePressed and not stock_long_is_braking
    planner_fcw = self.sm['longitudinalPlan'].fcw and self.enabled
    if planner_fcw or model_fcw:
      self.events.add(EventName.fcw)
      self.fcw_random_event_triggered = True
    elif self.fcw_random_event_triggered and self.random_events:
      self.events.add(EventName.yourFrogTriedToKillMe)
      self.fcw_random_event_triggered = False

    for m in messaging.drain_sock(self.log_sock, wait_for_one=False):
      try:
        msg = m.androidLog.message
        if any(err in msg for err in ("ERROR_CRC", "ERROR_ECC", "ERROR_STREAM_UNDERFLOW", "APPLY FAILED")):
          csid = msg.split("CSID:")[-1].split(" ")[0]
          evt = CSID_MAP.get(csid, None)
          if evt is not None:
            self.events.add(evt)
      except UnicodeDecodeError:
        pass

    # TODO: fix simulator
    if not SIMULATION or REPLAY:
      # Not show in first 1 km to allow for driving out of garage. This event shows after 5 minutes
      if not self.sm['liveLocationKalman'].gpsOK and self.sm['liveLocationKalman'].inputsOK and (self.distance_traveled > 1000):
        self.events.add(EventName.noGps)
      if self.sm['liveLocationKalman'].gpsOK:
        self.distance_traveled = 0
      self.distance_traveled += CS.vEgo * DT_CTRL

      if self.sm['modelV2'].frameDropPerc > 20:
        self.events.add(EventName.modeldLagging)

    self.update_frogpilot_events(CS)

  def data_sample(self):
    """Receive data from sockets and update carState"""

    CS = self.card.state_update(self.frogpilot_variables)

    self.sm.update(0)

    if not self.initialized:
      all_valid = CS.canValid and self.sm.all_checks()
      timed_out = self.sm.frame * DT_CTRL > 6.
      if all_valid or timed_out or (SIMULATION and not REPLAY):
        available_streams = VisionIpcClient.available_streams("camerad", block=False)
        if VisionStreamType.VISION_STREAM_ROAD not in available_streams:
          self.sm.ignore_alive.append('roadCameraState')
        if VisionStreamType.VISION_STREAM_WIDE_ROAD not in available_streams:
          self.sm.ignore_alive.append('wideRoadCameraState')

        if not self.CP.passive:
          self.card.initialize()

        self.initialized = True
        self.set_initial_state()
        self.params.put_bool_nonblocking("ControlsReady", True)

        cloudlog.event(
          "controlsd.initialized",
          dt=self.sm.frame*DT_CTRL,
          timeout=timed_out,
          canValid=CS.canValid,
          invalid=[s for s, valid in self.sm.valid.items() if not valid],
          not_alive=[s for s, alive in self.sm.alive.items() if not alive],
          not_freq_ok=[s for s, freq_ok in self.sm.freq_ok.items() if not freq_ok],
          error=True,
        )

    # When the panda and controlsd do not agree on controls_allowed
    # we want to disengage openpilot. However the status from the panda goes through
    # another socket other than the CAN messages and one can arrive earlier than the other.
    # Therefore we allow a mismatch for two samples, then we trigger the disengagement.
    if not self.enabled:
      self.mismatch_counter = 0

    # All pandas not in silent mode must have controlsAllowed when openpilot is enabled
    if self.enabled and any(not ps.controlsAllowed for ps in self.sm['pandaStates']
           if ps.safetyModel not in IGNORED_SAFETY_MODES):
      self.mismatch_counter += 1

    return CS

  def state_transition(self, CS):
    """Compute conditional state transitions and execute actions on state transitions"""

    self.v_cruise_helper.update_v_cruise(CS, self.enabled, self.is_metric, self.FPCC.speedLimitChanged, self.frogpilot_variables)

    # decrement the soft disable timer at every step, as it's reset on
    # entrance in SOFT_DISABLING state
    self.soft_disable_timer = max(0, self.soft_disable_timer - 1)

    self.current_alert_types = [ET.PERMANENT]

    # ENABLED, SOFT DISABLING, PRE ENABLING, OVERRIDING
    if self.state != State.disabled:
      # user and immediate disable always have priority in a non-disabled state
      if self.events.contains(ET.USER_DISABLE):
        self.state = State.disabled
        self.current_alert_types.append(ET.USER_DISABLE)

      elif self.events.contains(ET.IMMEDIATE_DISABLE):
        self.state = State.disabled
        self.current_alert_types.append(ET.IMMEDIATE_DISABLE)

      else:
        # ENABLED
        if self.state == State.enabled:
          if self.events.contains(ET.SOFT_DISABLE):
            self.state = State.softDisabling
            self.soft_disable_timer = int(SOFT_DISABLE_TIME / DT_CTRL)
            self.current_alert_types.append(ET.SOFT_DISABLE)

          elif self.events.contains(ET.OVERRIDE_LATERAL) or self.events.contains(ET.OVERRIDE_LONGITUDINAL):
            self.state = State.overriding
            self.current_alert_types += [ET.OVERRIDE_LATERAL, ET.OVERRIDE_LONGITUDINAL]

        # SOFT DISABLING
        elif self.state == State.softDisabling:
          if not self.events.contains(ET.SOFT_DISABLE):
            # no more soft disabling condition, so go back to ENABLED
            self.state = State.enabled

          elif self.soft_disable_timer > 0:
            self.current_alert_types.append(ET.SOFT_DISABLE)

          elif self.soft_disable_timer <= 0:
            self.state = State.disabled

        # PRE ENABLING
        elif self.state == State.preEnabled:
          if not self.events.contains(ET.PRE_ENABLE):
            self.state = State.enabled
          else:
            self.current_alert_types.append(ET.PRE_ENABLE)

        # OVERRIDING
        elif self.state == State.overriding:
          if self.events.contains(ET.SOFT_DISABLE):
            self.state = State.softDisabling
            self.soft_disable_timer = int(SOFT_DISABLE_TIME / DT_CTRL)
            self.current_alert_types.append(ET.SOFT_DISABLE)
          elif not (self.events.contains(ET.OVERRIDE_LATERAL) or self.events.contains(ET.OVERRIDE_LONGITUDINAL)):
            self.state = State.enabled
          else:
            self.current_alert_types += [ET.OVERRIDE_LATERAL, ET.OVERRIDE_LONGITUDINAL]

    # DISABLED
    elif self.state == State.disabled:
      if self.events.contains(ET.ENABLE):
        if self.events.contains(ET.NO_ENTRY):
          self.current_alert_types.append(ET.NO_ENTRY)

        else:
          if self.events.contains(ET.PRE_ENABLE):
            self.state = State.preEnabled
          elif self.events.contains(ET.OVERRIDE_LATERAL) or self.events.contains(ET.OVERRIDE_LONGITUDINAL):
            self.state = State.overriding
          else:
            self.state = State.enabled
          self.current_alert_types.append(ET.ENABLE)
          self.v_cruise_helper.initialize_v_cruise(CS, self.experimental_mode, self.sm['frogpilotPlan'].unconfirmedSlcSpeedLimit, self.frogpilot_variables)

    # Check if openpilot is engaged and actuators are enabled
    self.enabled = self.state in ENABLED_STATES
    self.active = self.state in ACTIVE_STATES
    if self.active:
      self.current_alert_types.append(ET.WARNING)

    if self.FPCC.alwaysOnLateral:
      self.current_alert_types.append(ET.WARNING)

  def state_control(self, CS):
    """Given the state, this function returns a CarControl packet"""

    # Update VehicleModel
    lp = self.sm['liveParameters']
    x = max(lp.stiffnessFactor, 0.1)
    sr = max(self.steer_ratio, 0.1) if self.use_custom_steer_ratio else max(lp.steerRatio, 0.1)
    self.VM.update_params(x, sr)

    # Update Torque Params
    if self.CP.lateralTuning.which() == 'torque':
      torque_params = self.sm['liveTorqueParameters']
      if self.sm.all_checks(['liveTorqueParameters']) and (torque_params.useParams or self.force_auto_tune):
        self.LaC.update_live_torque_params(torque_params.latAccelFactorFiltered, torque_params.latAccelOffsetFiltered,
                                           torque_params.frictionCoefficientFiltered)

    long_plan = self.sm['longitudinalPlan']
    model_v2 = self.sm['modelV2']

    CC = car.CarControl.new_message()
    CC.enabled = self.enabled

    # Check which actuators can be enabled
    standstill = CS.vEgo <= max(self.CP.minSteerSpeed, MIN_LATERAL_CONTROL_SPEED) or CS.standstill
    CC.latActive = (self.active or self.FPCC.alwaysOnLateral) and self.speed_check and not CS.steerFaultTemporary and not CS.steerFaultPermanent and \
                   (not standstill or self.joystick_mode)
    CC.longActive = self.enabled and not self.events.contains(ET.OVERRIDE_LONGITUDINAL) and self.CP.openpilotLongitudinalControl

    actuators = CC.actuators
    actuators.longControlState = self.LoC.long_control_state

    # Enable blinkers while lane changing
    if model_v2.meta.laneChangeState != LaneChangeState.off:
      CC.leftBlinker = model_v2.meta.laneChangeDirection == LaneChangeDirection.left
      CC.rightBlinker = model_v2.meta.laneChangeDirection == LaneChangeDirection.right

    if CS.leftBlinker or CS.rightBlinker:
      self.last_blinker_frame = self.sm.frame

    # State specific actions

    if not CC.latActive:
      self.LaC.reset()
    if not CC.longActive:
      self.LoC.reset(v_pid=CS.vEgo)

    if not self.joystick_mode:
      # accel PID loop
      pid_accel_limits = self.CI.get_pid_accel_limits(self.CP, CS.vEgo, self.v_cruise_helper.v_cruise_kph * CV.KPH_TO_MS, self.frogpilot_variables)
      t_since_plan = (self.sm.frame - self.sm.recv_frame['longitudinalPlan']) * DT_CTRL
      actuators.accel = self.LoC.update(CC.longActive, CS, long_plan, pid_accel_limits, t_since_plan)

      if len(long_plan.speeds):
        actuators.speed = long_plan.speeds[-1]

      # Steering PID loop and lateral MPC
      self.desired_curvature = clip_curvature(CS.vEgo, self.desired_curvature, model_v2.action.desiredCurvature)
      actuators.curvature = self.desired_curvature
      actuators.steer, actuators.steeringAngleDeg, lac_log = self.LaC.update(CC.latActive, CS, self.VM, lp,
                                                                             self.steer_limited, self.desired_curvature,
                                                                             self.sm['liveLocationKalman'],
                                                                             model_data=self.sm['modelV2'])
    else:
      lac_log = log.ControlsState.LateralDebugState.new_message()
      if self.sm.recv_frame['testJoystick'] > 0:
        # reset joystick if it hasn't been received in a while
        should_reset_joystick = (self.sm.frame - self.sm.recv_frame['testJoystick'])*DT_CTRL > 0.2
        if not should_reset_joystick:
          joystick_axes = self.sm['testJoystick'].axes
        else:
          joystick_axes = [0.0, 0.0]

        if CC.longActive:
          actuators.accel = 4.0*clip(joystick_axes[0], -1, 1)

        if CC.latActive:
          steer = clip(joystick_axes[1], -1, 1)
          # max angle is 45 for angle-based cars, max curvature is 0.02
          actuators.steer, actuators.steeringAngleDeg, actuators.curvature = steer, steer * 90., steer * -0.02

        lac_log.active = self.active
        lac_log.steeringAngleDeg = CS.steeringAngleDeg
        lac_log.output = actuators.steer
        lac_log.saturated = abs(actuators.steer) >= 0.9

    if CS.steeringPressed:
      self.last_steering_pressed_frame = self.sm.frame
    recent_steer_pressed = (self.sm.frame - self.last_steering_pressed_frame)*DT_CTRL < 2.0

    # Send a "steering required alert" if saturation count has reached the limit
    if lac_log.active and not recent_steer_pressed and not self.CP.notCar:
      if self.CP.lateralTuning.which() == 'torque' and not self.joystick_mode:
        undershooting = abs(lac_log.desiredLateralAccel) / abs(1e-3 + lac_log.actualLateralAccel) > 1.2
        turning = abs(lac_log.desiredLateralAccel) > 1.0
        good_speed = CS.vEgo > 5
        max_torque = abs(actuators.steer) > 0.99
        if undershooting and turning and good_speed and max_torque and not self.random_event_triggered:
          if self.sm.frame % 10000 == 0 and self.random_events:
            lac_log.active and self.events.add(EventName.firefoxSteerSaturated)
            self.params_memory.put_int("CurrentRandomEvent", 1)
            self.random_event_triggered = True
          else:
            lac_log.active and self.events.add(EventName.goatSteerSaturated if self.goat_scream else EventName.steerSaturated)
      elif lac_log.saturated:
        # TODO probably should not use dpath_points but curvature
        dpath_points = model_v2.position.y
        if len(dpath_points):
          # Check if we deviated from the path
          # TODO use desired vs actual curvature
          if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
            steering_value = actuators.steeringAngleDeg
          else:
            steering_value = actuators.steer

          left_deviation = steering_value > 0 and dpath_points[0] < -0.20
          right_deviation = steering_value < 0 and dpath_points[0] > 0.20

          if left_deviation or right_deviation:
            self.events.add(EventName.steerSaturated)

    # Ensure no NaNs/Infs
    for p in ACTUATOR_FIELDS:
      attr = getattr(actuators, p)
      if not isinstance(attr, SupportsFloat):
        continue

      if not math.isfinite(attr):
        cloudlog.error(f"actuators.{p} not finite {actuators.to_dict()}")
        setattr(actuators, p, 0.0)

    # decrement personality on distance button press
    if self.CP.openpilotLongitudinalControl:
      if any(not be.pressed and be.type == ButtonType.gapAdjustCruise for be in CS.buttonEvents) or self.onroad_distance_pressed:
        if not (self.params_memory.get_bool("DistanceLongPressed") or self.params_memory.get_bool("OnroadDistanceButtonPressed")):
          self.personality = (self.personality - 1) % 3
          self.params.put_nonblocking('LongitudinalPersonality', str(self.personality))
      self.onroad_distance_pressed = self.params_memory.get_bool("OnroadDistanceButtonPressed")

    return CC, lac_log

  def publish_logs(self, CS, start_time, CC, lac_log):
    """Send actuators and hud commands to the car, send controlsstate and MPC logging"""

    CO = self.sm['carOutput']

    # Orientation and angle rates can be useful for carcontroller
    # Only calibrated (car) frame is relevant for the carcontroller
    orientation_value = list(self.sm['liveLocationKalman'].calibratedOrientationNED.value)
    if len(orientation_value) > 2:
      CC.orientationNED = orientation_value
    angular_rate_value = list(self.sm['liveLocationKalman'].angularVelocityCalibrated.value)
    if len(angular_rate_value) > 2:
      CC.angularVelocity = angular_rate_value

    CC.cruiseControl.override = self.enabled and not CC.longActive and self.CP.openpilotLongitudinalControl
    CC.cruiseControl.cancel = CS.cruiseState.enabled and (not self.enabled or not self.CP.pcmCruise)
    if self.joystick_mode and self.sm.recv_frame['testJoystick'] > 0 and self.sm['testJoystick'].buttons[0]:
      CC.cruiseControl.cancel = True

    speeds = self.sm['longitudinalPlan'].speeds
    if len(speeds):
      CC.cruiseControl.resume = self.enabled and CS.cruiseState.standstill and speeds[-1] > 0.1

    hudControl = CC.hudControl
    hudControl.setSpeed = float(self.v_cruise_helper.v_cruise_cluster_kph * CV.KPH_TO_MS)
    hudControl.speedVisible = self.enabled
    hudControl.lanesVisible = self.enabled
    hudControl.leadVisible = self.sm['longitudinalPlan'].hasLead
    hudControl.leadDistanceBars = self.personality + 1

    hudControl.rightLaneVisible = True
    hudControl.leftLaneVisible = True

    recent_blinker = (self.sm.frame - self.last_blinker_frame) * DT_CTRL < 5.0  # 5s blinker cooldown
    ldw_allowed = self.is_ldw_enabled and CS.vEgo > LDW_MIN_SPEED and not recent_blinker \
                  and not CC.latActive and self.sm['liveCalibration'].calStatus == log.LiveCalibrationData.Status.calibrated

    model_v2 = self.sm['modelV2']
    desire_prediction = model_v2.meta.desirePrediction
    if len(desire_prediction) and ldw_allowed:
      right_lane_visible = model_v2.laneLineProbs[2] > 0.5
      left_lane_visible = model_v2.laneLineProbs[1] > 0.5
      l_lane_change_prob = desire_prediction[Desire.laneChangeLeft]
      r_lane_change_prob = desire_prediction[Desire.laneChangeRight]

      lane_lines = model_v2.laneLines
      l_lane_close = left_lane_visible and (lane_lines[1].y[0] > -(1.08 + CAMERA_OFFSET))
      r_lane_close = right_lane_visible and (lane_lines[2].y[0] < (1.08 - CAMERA_OFFSET))

      hudControl.leftLaneDepart = bool(l_lane_change_prob > LANE_DEPARTURE_THRESHOLD and l_lane_close)
      hudControl.rightLaneDepart = bool(r_lane_change_prob > LANE_DEPARTURE_THRESHOLD and r_lane_close)

    if hudControl.rightLaneDepart or hudControl.leftLaneDepart:
      self.events.add(EventName.ldw)

    clear_event_types = set()
    if ET.WARNING not in self.current_alert_types:
      clear_event_types.add(ET.WARNING)
    if self.enabled:
      clear_event_types.add(ET.NO_ENTRY)

    alerts = self.events.create_alerts(self.current_alert_types, [self.CP, CS, self.sm, self.is_metric, self.soft_disable_timer])
    self.AM.add_many(self.sm.frame, alerts)
    current_alert = self.AM.process_alerts(self.sm.frame, clear_event_types)
    if current_alert:
      hudControl.visualAlert = current_alert.visual_alert

    if not self.CP.passive and self.initialized:
      self.card.controls_update(CC, self.frogpilot_variables)
      if self.CP.steerControlType == car.CarParams.SteerControlType.angle:
        self.steer_limited = abs(CC.actuators.steeringAngleDeg - CO.actuatorsOutput.steeringAngleDeg) > \
                             STEER_ANGLE_SATURATION_THRESHOLD
      else:
        self.steer_limited = abs(CC.actuators.steer - CO.actuatorsOutput.steer) > 1e-2

    force_decel = (self.sm['driverMonitoringState'].awarenessStatus < 0.) or \
                  (self.state == State.softDisabling)

    # Curvature & Steering angle
    lp = self.sm['liveParameters']

    steer_angle_without_offset = math.radians(CS.steeringAngleDeg - lp.angleOffsetDeg)
    curvature = -self.VM.calc_curvature(steer_angle_without_offset, CS.vEgo, lp.roll)

    # controlsState
    dat = messaging.new_message('controlsState')
    dat.valid = CS.canValid
    controlsState = dat.controlsState
    if current_alert:
      controlsState.alertText1 = current_alert.alert_text_1
      controlsState.alertText2 = current_alert.alert_text_2
      controlsState.alertSize = current_alert.alert_size
      controlsState.alertStatus = current_alert.alert_status
      controlsState.alertBlinkingRate = current_alert.alert_rate
      controlsState.alertType = current_alert.alert_type
      controlsState.alertSound = current_alert.audible_alert

    controlsState.longitudinalPlanMonoTime = self.sm.logMonoTime['longitudinalPlan']
    controlsState.lateralPlanMonoTime = self.sm.logMonoTime['modelV2']
    controlsState.enabled = self.enabled
    controlsState.active = self.active
    controlsState.curvature = curvature
    controlsState.desiredCurvature = self.desired_curvature
    controlsState.state = self.state
    controlsState.engageable = not self.events.contains(ET.NO_ENTRY)
    controlsState.longControlState = self.LoC.long_control_state
    controlsState.vPid = float(self.LoC.v_pid)
    controlsState.vCruise = float(self.v_cruise_helper.v_cruise_kph)
    controlsState.vCruiseCluster = float(self.v_cruise_helper.v_cruise_cluster_kph)
    controlsState.upAccelCmd = float(self.LoC.pid.p)
    controlsState.uiAccelCmd = float(self.LoC.pid.i)
    controlsState.ufAccelCmd = float(self.LoC.pid.f)
    controlsState.cumLagMs = -self.rk.remaining * 1000.
    controlsState.startMonoTime = int(start_time * 1e9)
    controlsState.forceDecel = bool(force_decel)
    controlsState.canErrorCounter = self.card.can_rcv_cum_timeout_counter
    controlsState.experimentalMode = self.experimental_mode
    controlsState.personality = self.personality

    lat_tuning = self.CP.lateralTuning.which()
    if self.joystick_mode:
      controlsState.lateralControlState.debugState = lac_log
    elif self.CP.steerControlType == car.CarParams.SteerControlType.angle:
      controlsState.lateralControlState.angleState = lac_log
    elif lat_tuning == 'pid':
      controlsState.lateralControlState.pidState = lac_log
    elif lat_tuning == 'torque':
      controlsState.lateralControlState.torqueState = lac_log

    self.pm.send('controlsState', dat)

    # onroadEvents - logged every second or on change
    if (self.sm.frame % int(1. / DT_CTRL) == 0) or (self.events.names != self.events_prev):
      ce_send = messaging.new_message('onroadEvents', len(self.events))
      ce_send.valid = True
      ce_send.onroadEvents = self.events.to_msg()
      self.pm.send('onroadEvents', ce_send)
    self.events_prev = self.events.names.copy()

    # carControl
    cc_send = messaging.new_message('carControl')
    cc_send.valid = CS.canValid
    cc_send.carControl = CC
    self.pm.send('carControl', cc_send)

    # copy CarControl to pass to CarInterface on the next iteration
    self.CC = CC

  def step(self):
    start_time = time.monotonic()

    # Sample data from sockets and get a carState
    CS = self.data_sample()
    cloudlog.timestamp("Data sampled")

    self.update_events(CS)
    cloudlog.timestamp("Events updated")

    if not self.CP.passive and self.initialized:
      # Update control state
      self.state_transition(CS)

    # Compute actuators (runs PID loops and lateral MPC)
    CC, lac_log = self.state_control(CS)

    # Publish data
    self.publish_logs(CS, start_time, CC, lac_log)

    self.CS_prev = CS

    # Update FrogPilot variables
    self.update_frogpilot_variables(CS)

  def read_personality_param(self):
    try:
      return int(self.params.get('LongitudinalPersonality'))
    except (ValueError, TypeError):
      return log.LongitudinalPersonality.standard

  def params_thread(self, evt):
    while not evt.is_set():
      self.is_metric = self.params.get_bool("IsMetric")
      if self.CP.openpilotLongitudinalControl and not self.frogpilot_variables.conditional_experimental_mode:
        self.experimental_mode = self.params.get_bool("ExperimentalMode") or self.speed_limit_controller and SpeedLimitController.experimental_mode
      self.personality = self.read_personality_param()
      if self.CP.notCar:
        self.joystick_mode = self.params.get_bool("JoystickDebugMode")
      time.sleep(0.1)

  def controlsd_thread(self):
    e = threading.Event()
    t = threading.Thread(target=self.params_thread, args=(e, ))
    try:
      t.start()
      while True:
        self.step()
        self.rk.monitor_time()
    except SystemExit:
      e.set()
      t.join()

  def update_frogpilot_events(self, CS):
    if self.block_user:
      self.events.add(EventName.blockUser)
      return

    if self.random_events:
      acceleration = CS.aEgo

      if not CS.gasPressed:
        self.max_acceleration = max(acceleration, self.max_acceleration)
      else:
        self.max_acceleration = 0

      if 3.5 > self.max_acceleration >= 3.0 and acceleration < 1.5:
        self.events.add(EventName.accel30)
        self.params_memory.put_int("CurrentRandomEvent", 2)
        self.random_event_triggered = True
        self.max_acceleration = 0

      elif 4.0 > self.max_acceleration >= 3.5 and acceleration < 1.5:
        self.events.add(EventName.accel35)
        self.params_memory.put_int("CurrentRandomEvent", 3)
        self.random_event_triggered = True
        self.max_acceleration = 0

      elif self.max_acceleration >= 4.0 and acceleration < 1.5:
        self.events.add(EventName.accel40)
        self.params_memory.put_int("CurrentRandomEvent", 4)
        self.random_event_triggered = True
        self.max_acceleration = 0

    if self.green_light_alert:
      green_light = not self.sm['frogpilotPlan'].redLight
      green_light &= not CS.gasPressed
      green_light &= not self.sm['longitudinalPlan'].hasLead
      green_light &= self.previously_enabled
      green_light &= CS.standstill

      self.green_light_mac.add_data(green_light)
      if self.green_light_mac.get_moving_average() >= PROBABILITY:
        self.events.add(EventName.greenLight)

    if self.sm.frame >= 1000 and self.holiday_themes and self.params_memory.get_int("CurrentHolidayTheme") != 0 and not self.holiday_theme_alerted:
      self.events.add(EventName.holidayActive)
      self.holiday_theme_alerted = True

    if self.lead_departing_alert and self.sm.frame % 50 == 0:
      lead = self.sm['radarState'].leadOne
      lead_distance = lead.dRel

      lead_departing = lead_distance - self.previous_lead_distance > 0.5 and self.previous_lead_distance != 0 and CS.standstill
      self.previous_lead_distance = lead_distance

      lead_departing &= not CS.gasPressed
      lead_departing &= lead.vLead > 1
      lead_departing &= self.driving_gear

      if lead_departing:
        self.events.add(EventName.leadDeparting)

    if not CS.standstill:
      if self.sm['modelV2'].meta.turnDirection == Desire.turnLeft:
        self.events.add(EventName.turningLeft)
      elif self.sm['modelV2'].meta.turnDirection == Desire.turnRight:
        self.events.add(EventName.turningRight)

    if os.path.isfile(os.path.join(sentry.CRASHES_DIR, 'error.txt')) and not self.openpilot_crashed_triggered:
      if self.random_events:
        self.events.add(EventName.openpilotCrashedRandomEvents)
      else:
        self.events.add(EventName.openpilotCrashed)

      self.openpilot_crashed_triggered = True

    if self.speed_limit_alert or self.speed_limit_confirmation:
      current_speed_limit = self.sm['frogpilotPlan'].slcSpeedLimit
      desired_speed_limit = self.sm['frogpilotPlan'].unconfirmedSlcSpeedLimit

      speed_limit_changed = desired_speed_limit != self.previous_speed_limit and abs(current_speed_limit - desired_speed_limit) > 1

      speed_limit_changed_lower = speed_limit_changed and self.previous_speed_limit > desired_speed_limit
      speed_limit_changed_higher = speed_limit_changed and self.previous_speed_limit < desired_speed_limit

      self.previous_speed_limit = desired_speed_limit

      if self.CP.pcmCruise and self.FPCC.speedLimitChanged:
        if any(be.type == ButtonType.accelCruise for be in CS.buttonEvents):
          self.params_memory.put_bool("SLCConfirmed", True)
          self.params_memory.put_bool("SLCConfirmedPressed", True)
        elif any(be.type == ButtonType.decelCruise for be in CS.buttonEvents):
          self.params_memory.put_bool("SLCConfirmed", False)
          self.params_memory.put_bool("SLCConfirmedPressed", True)

      if speed_limit_changed_lower:
        if self.speed_limit_confirmation_lower:
          self.FPCC.speedLimitChanged = True
        else:
          self.params_memory.put_bool("SLCConfirmed", True)
      elif speed_limit_changed_higher:
        if self.speed_limit_confirmation_higher:
          self.FPCC.speedLimitChanged = True
        else:
          self.params_memory.put_bool("SLCConfirmed", True)

      if self.params_memory.get_bool("SLCConfirmedPressed") or not self.speed_limit_confirmation:
        self.FPCC.speedLimitChanged = False
        self.params_memory.put_bool("SLCConfirmedPressed", False)

      if (speed_limit_changed_lower or speed_limit_changed_higher) and self.speed_limit_alert:
        self.events.add(EventName.speedLimitChanged)

      if self.FPCC.speedLimitChanged:
        self.speed_limit_timer += DT_CTRL
        if self.speed_limit_timer >= 10:
          self.FPCC.speedLimitChanged = False
          self.speed_limit_timer = 0
      else:
        self.speed_limit_timer = 0
    else:
      self.FPCC.speedLimitChanged = False

    if self.sm.frame == 550 and self.CP.lateralTuning.which() == 'torque' and self.CI.use_nnff:
      self.events.add(EventName.torqueNNLoad)

    if self.random_events:
      conversion = 1 if self.is_metric else CV.KPH_TO_MPH
      v_cruise = max(self.v_cruise_helper.v_cruise_cluster_kph, self.v_cruise_helper.v_cruise_kph) * conversion

      if 70 > v_cruise >= 69:
        if not self.vCruise69_alert_played:
          self.events.add(EventName.vCruise69)
          self.vCruise69_alert_played = True
      else:
        self.vCruise69_alert_played = False

  def update_frogpilot_variables(self, CS):
    self.driving_gear = CS.gearShifter not in (GearShifter.neutral, GearShifter.park, GearShifter.reverse, GearShifter.unknown)

    self.FPCC.alwaysOnLateral |= CS.cruiseState.enabled or self.always_on_lateral_main
    self.FPCC.alwaysOnLateral &= CS.cruiseState.available
    self.FPCC.alwaysOnLateral &= self.always_on_lateral
    self.FPCC.alwaysOnLateral &= self.driving_gear
    self.FPCC.alwaysOnLateral &= self.speed_check
    self.FPCC.alwaysOnLateral &= not (CS.brakePressed and CS.vEgo < self.always_on_lateral_pause_speed) or CS.standstill

    if self.CP.openpilotLongitudinalControl and self.frogpilot_variables.conditional_experimental_mode:
      self.experimental_mode = self.sm['frogpilotPlan'].conditionalExperimental

    self.drive_distance += CS.vEgo * DT_CTRL
    self.drive_time += DT_CTRL

    if self.drive_time > 60 and CS.standstill:
      current_total_distance = self.params.get_float("FrogPilotKilometers")
      distance_to_add = self.drive_distance / 1000
      new_total_distance = current_total_distance + distance_to_add

      self.params.put_float_nonblocking("FrogPilotKilometers", new_total_distance)
      self.params_storage.put_float_nonblocking("FrogPilotKilometers", new_total_distance)

      self.drive_distance = 0

      current_total_time = self.params.get_float("FrogPilotMinutes")
      time_to_add = self.drive_time / 60
      new_total_time = current_total_time + time_to_add

      self.params.put_float_nonblocking("FrogPilotMinutes", new_total_time)
      self.params_storage.put_float_nonblocking("FrogPilotMinutes", new_total_time)

      self.drive_time = 0

      if self.sm.frame * DT_CTRL > 60 * 5 and not self.drive_added:
        new_total_drives = self.params.get_int("FrogPilotDrives") + 1

        self.params.put_int_nonblocking("FrogPilotDrives", new_total_drives)
        self.params_storage.put_int_nonblocking("FrogPilotDrives", new_total_drives)

        self.drive_added = True

    if any(be.pressed and be.type == FrogPilotButtonType.lkas for be in CS.buttonEvents) and self.experimental_mode_via_lkas:
      if self.frogpilot_variables.conditional_experimental_mode:
        conditional_status = self.params_memory.get_int("CEStatus")
        override_value = 0 if conditional_status in {1, 2, 3, 4, 5, 6} else 3 if conditional_status >= 7 else 4
        self.params_memory.put_int("CEStatus", override_value)
      else:
        self.params.put_bool_nonblocking("ExperimentalMode", not self.experimental_mode)

    self.previously_enabled |= (self.enabled or self.FPCC.alwaysOnLateral) and CS.vEgo > CRUISING_SPEED
    self.previously_enabled &= self.driving_gear

    if self.random_event_triggered:
      self.random_event_timer += DT_CTRL
      if self.random_event_timer >= 4:
        self.random_event_triggered = False
        self.random_event_timer = 0
        self.params_memory.remove("CurrentRandomEvent")

    signal_check = CS.vEgo >= self.pause_lateral_below_speed or not (CS.leftBlinker or CS.rightBlinker) or CS.standstill
    self.speed_check = CS.vEgo >= self.pause_lateral_below_speed or CS.standstill or signal_check and self.pause_lateral_below_signal

    self.FPCC.trafficModeActive = self.frogpilot_variables.traffic_mode and self.params_memory.get_bool("TrafficModeActive")

    fpcc_send = messaging.new_message('frogpilotCarControl')
    fpcc_send.valid = CS.canValid
    fpcc_send.frogpilotCarControl = self.FPCC
    self.pm.send('frogpilotCarControl', fpcc_send)

    if self.params_memory.get_bool("FrogPilotTogglesUpdated"):
      self.update_frogpilot_params()

  def update_frogpilot_params(self):
    self.always_on_lateral_pause_speed = self.always_on_lateral and self.params.get_int("PauseAOLOnBrake")

    self.frogpilot_variables.conditional_experimental_mode = self.CP.openpilotLongitudinalControl and self.params.get_bool("ConditionalExperimental")

    custom_alerts = self.params.get_bool("CustomAlerts")
    self.green_light_alert = custom_alerts and self.params.get_bool("GreenLightAlert")
    self.lead_departing_alert = not self.radarless_model and custom_alerts and self.params.get_bool("LeadDepartingAlert")
    self.loud_blindspot_alert = custom_alerts and self.params.get_bool("LoudBlindspotAlert")

    custom_theme = self.params.get_bool("CustomTheme")
    custom_sounds = self.params.get_int("CustomSounds") if custom_theme else 0
    frog_sounds = custom_sounds == 1
    self.goat_scream = frog_sounds and self.params.get_bool("GoatScream")
    self.holiday_themes = custom_theme and self.params.get_bool("HolidayThemes")
    self.random_events = custom_theme and self.params.get_bool("RandomEvents")

    device_management = self.params.get_bool("DeviceManagement")
    self.increase_thermal_limits = device_management and self.params.get_bool("IncreaseThermalLimits")

    experimental_mode_activation = self.CP.openpilotLongitudinalControl and self.params.get_bool("ExperimentalModeActivation")
    self.frogpilot_variables.experimental_mode_via_distance = experimental_mode_activation and self.params.get_bool("ExperimentalModeViaDistance")
    self.experimental_mode_via_lkas = experimental_mode_activation and self.params.get_bool("ExperimentalModeViaLKAS")

    lane_detection = self.params.get_bool("NudgelessLaneChange") and self.params.get_int("LaneDetectionWidth") != 0
    self.lane_detection_width = self.params.get_int("LaneDetectionWidth") * (1 if self.is_metric else CV.FOOT_TO_METER) / 10 if lane_detection else 0

    lateral_tune = self.params.get_bool("LateralTune")
    self.force_auto_tune = lateral_tune and self.params.get_float("ForceAutoTune")
    stock_steer_ratio = self.params.get_float("SteerRatioStock")
    self.steer_ratio = self.params.get_float("SteerRatio") if lateral_tune else stock_steer_ratio
    self.use_custom_steer_ratio = self.steer_ratio != stock_steer_ratio

    self.frogpilot_variables.long_pitch = self.params.get_bool("LongPitch")

    longitudinal_tune = self.CP.openpilotLongitudinalControl and self.params.get_bool("LongitudinalTune")
    self.frogpilot_variables.sport_plus = longitudinal_tune and self.params.get_int("AccelerationProfile") == 3
    self.frogpilot_variables.traffic_mode = longitudinal_tune and self.params.get_bool("TrafficMode")

    quality_of_life = self.params.get_bool("QOLControls")
    self.frogpilot_variables.custom_cruise_increase = self.params.get_int("CustomCruise") if quality_of_life else 1
    self.frogpilot_variables.custom_cruise_increase_long = self.params.get_int("CustomCruiseLong") if quality_of_life else 5
    self.pause_lateral_below_speed = self.params.get_int("PauseLateralSpeed") * (CV.KPH_TO_MS if self.is_metric else CV.MPH_TO_MS) if quality_of_life else 0
    self.pause_lateral_below_signal = quality_of_life and self.params.get_bool("PauseLateralOnSignal")
    self.frogpilot_variables.reverse_cruise_increase = quality_of_life and self.params.get_bool("ReverseCruise")
    self.frogpilot_variables.set_speed_offset = self.params.get_int("SetSpeedOffset") * (1 if self.is_metric else CV.MPH_TO_KPH) if quality_of_life else 0

    self.frogpilot_variables.sng_hack = self.params.get_bool("SNGHack")

    self.speed_limit_controller = self.CP.openpilotLongitudinalControl and self.params.get_bool("SpeedLimitController")
    self.frogpilot_variables.force_mph_dashboard = self.speed_limit_controller and self.params.get_bool("ForceMPHDashboard")
    self.frogpilot_variables.set_speed_limit = self.speed_limit_controller and self.params.get_bool("SetSpeedLimit")
    self.speed_limit_alert = self.speed_limit_controller and self.params.get_bool("SpeedLimitChangedAlert")
    self.speed_limit_confirmation = self.speed_limit_controller and self.params.get_bool("SLCConfirmation")
    self.speed_limit_confirmation_lower = self.speed_limit_confirmation and self.params.get_bool("SLCConfirmationLower")
    self.speed_limit_confirmation_higher = self.speed_limit_confirmation and self.params.get_bool("SLCConfirmationHigher")

    toyota_doors = self.params.get_bool("ToyotaDoors")
    self.frogpilot_variables.lock_doors = toyota_doors and self.params.get_bool("LockDoors")
    self.frogpilot_variables.unlock_doors = toyota_doors and self.params.get_bool("UnlockDoors")

    self.frogpilot_variables.use_ev_tables = self.params.get_bool("EVTable")

def main():
  config_realtime_process(4, Priority.CTRL_HIGH)
  controls = Controls()
  controls.controlsd_thread()


if __name__ == "__main__":
  main()