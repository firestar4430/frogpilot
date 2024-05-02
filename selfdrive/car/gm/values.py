from dataclasses import dataclass, field
from enum import Enum, IntFlag

from cereal import car
from openpilot.common.numpy_fast import interp
from openpilot.common.params import Params
from openpilot.selfdrive.car import dbc_dict, PlatformConfig, DbcDict, Platforms, CarSpecs
from openpilot.selfdrive.car.docs_definitions import CarFootnote, CarHarness, CarDocs, CarParts, Column
from openpilot.selfdrive.car.fw_query_definitions import FwQueryConfig, Request, StdQueries

Ecu = car.CarParams.Ecu


class CarControllerParams:
  STEER_MAX = 300  # GM limit is 3Nm. Used by carcontroller to generate LKA output
  STEER_STEP = 3  # Active control frames per command (~33hz)
  INACTIVE_STEER_STEP = 10  # Inactive control frames per command (10hz)
  STEER_DELTA_UP = 10  # Delta rates require review due to observed EPS weakness
  STEER_DELTA_DOWN = 15
  STEER_DRIVER_ALLOWANCE = 65
  STEER_DRIVER_MULTIPLIER = 4
  STEER_DRIVER_FACTOR = 100
  NEAR_STOP_BRAKE_PHASE = 0.25  # m/s
  SNG_INTERCEPTOR_GAS = 18. / 255.
  SNG_TIME = 30  # frames until the above is reached

  # Heartbeat for dash "Service Adaptive Cruise" and "Service Front Camera"
  ADAS_KEEPALIVE_STEP = 100
  CAMERA_KEEPALIVE_STEP = 100

  # Allow small margin below -3.5 m/s^2 from ISO 15622:2018 since we
  # perform the closed loop control, and might need some
  # to apply some more braking if we're on a downhill slope.
  # Our controller should still keep the 2 second average above
  # -3.5 m/s^2 as per planner limits
  ACCEL_MAX = 2.  # m/s^2
  ACCEL_MAX_PLUS = 4.  # m/s^2
  ACCEL_MIN = -4.  # m/s^2

  def __init__(self, CP):
    # Gas/brake lookups
    self.ZERO_GAS = 6144  # Coasting
    self.MAX_BRAKE = 400  # ~ -4.0 m/s^2 with regen

    if CP.carFingerprint in CAMERA_ACC_CAR and CP.carFingerprint not in CC_ONLY_CAR:
      self.MAX_GAS = 7496
      self.MAX_GAS_PLUS = 8848
      self.MAX_ACC_REGEN = 5610
      self.INACTIVE_REGEN = 5650
      # Camera ACC vehicles have no regen while enabled.
      # Camera transitions to MAX_ACC_REGEN from ZERO_GAS and uses friction brakes instantly
      max_regen_acceleration = 0.

      if CP.carFingerprint in SLOW_ACC and Params().get_bool("GasRegenCmd"):
        self.MAX_GAS = 8650
        self.MAX_GAS_PLUS = 8650 # Don't Stack Extra Speed
        self.ACCEL_MAX_PLUS = 2

    elif CP.carFingerprint in SDGM_CAR:
      self.MAX_GAS = 7496
      self.MAX_GAS_PLUS = 8848
      self.MAX_ACC_REGEN = 5610
      self.INACTIVE_REGEN = 5650
      max_regen_acceleration = 0.

    else:
      self.MAX_GAS = 7168  # Safety limit, not ACC max. Stock ACC >8192 from standstill.
      self.MAX_GAS_PLUS = 8191 # 8292 uses new bit, possible but not tested. Matches Twilsonco tw-main max
      self.MAX_ACC_REGEN = 5500  # Max ACC regen is slightly less than max paddle regen
      self.INACTIVE_REGEN = 5500
      # ICE has much less engine braking force compared to regen in EVs,
      # lower threshold removes some braking deadzone
      max_regen_acceleration = -1. if CP.carFingerprint in EV_CAR else -0.1

    self.GAS_LOOKUP_BP = [max_regen_acceleration, 0., self.ACCEL_MAX]
    self.GAS_LOOKUP_BP_PLUS = [max_regen_acceleration, 0., self.ACCEL_MAX_PLUS]
    self.GAS_LOOKUP_V = [self.MAX_ACC_REGEN, self.ZERO_GAS, self.MAX_GAS]
    self.GAS_LOOKUP_V_PLUS = [self.MAX_ACC_REGEN, self.ZERO_GAS, self.MAX_GAS_PLUS]

    self.BRAKE_LOOKUP_BP = [self.ACCEL_MIN, max_regen_acceleration]
    self.BRAKE_LOOKUP_V = [self.MAX_BRAKE, 0.]

  # determined by letting Volt regen to a stop in L gear from 89mph,
  # and by letting off gas and allowing car to creep, for determining
  # the positive threshold values at very low speed
  EV_GAS_BRAKE_THRESHOLD_BP = [1.29, 1.52, 1.55, 1.6, 1.7, 1.8, 2.0, 2.2, 2.5, 5.52, 9.6, 20.5, 23.5, 35.0] # [m/s]
  EV_GAS_BRAKE_THRESHOLD_V = [0.0, -0.14, -0.16, -0.18, -0.215, -0.255, -0.32, -0.41, -0.5, -0.72, -0.895, -1.125, -1.145, -1.16] # [m/s^s]

  def update_ev_gas_brake_threshold(self, v_ego):
    gas_brake_threshold = interp(v_ego, self.EV_GAS_BRAKE_THRESHOLD_BP, self.EV_GAS_BRAKE_THRESHOLD_V)
    self.EV_GAS_LOOKUP_BP = [gas_brake_threshold, max(0., gas_brake_threshold), self.ACCEL_MAX]
    self.EV_GAS_LOOKUP_BP_PLUS = [gas_brake_threshold, max(0., gas_brake_threshold), self.ACCEL_MAX_PLUS]
    self.EV_BRAKE_LOOKUP_BP = [self.ACCEL_MIN, gas_brake_threshold]


class Footnote(Enum):
  OBD_II = CarFootnote(
    'Requires a <a href="https://github.com/commaai/openpilot/wiki/GM#hardware" target="_blank">community built ASCM harness</a>. ' +
    '<b><i>NOTE: disconnecting the ASCM disables Automatic Emergency Braking (AEB).</i></b>',
    Column.MODEL)


@dataclass
class GMCarDocs(CarDocs):
  package: str = "Adaptive Cruise Control (ACC)"

  def init_make(self, CP: car.CarParams):
    if CP.networkLocation == car.CarParams.NetworkLocation.fwdCamera:
      self.car_parts = CarParts.common([CarHarness.gm])
    else:
      self.car_parts = CarParts.common([CarHarness.obd_ii])
      self.footnotes.append(Footnote.OBD_II)


@dataclass(frozen=True, kw_only=True)
class GMCarSpecs(CarSpecs):
  tireStiffnessFactor: float = 0.444  # not optimized yet


@dataclass
class GMPlatformConfig(PlatformConfig):
  dbc_dict: DbcDict = field(default_factory=lambda: dbc_dict('gm_global_a_powertrain_generated', 'gm_global_a_object', chassis_dbc='gm_global_a_chassis'))


class CAR(Platforms):
  HOLDEN_ASTRA = GMPlatformConfig(
    "HOLDEN ASTRA RS-V BK 2017",
    [GMCarDocs("Holden Astra 2017")],
    GMCarSpecs(mass=1363, wheelbase=2.662, steerRatio=15.7, centerToFrontRatio=0.4),
  )
  VOLT = GMPlatformConfig(
    "CHEVROLET VOLT PREMIER 2017",
    [GMCarDocs("Chevrolet Volt 2017-18", min_enable_speed=0, video_link="https://youtu.be/QeMCN_4TFfQ")],
    GMCarSpecs(mass=1607, wheelbase=2.69, steerRatio=17.7, centerToFrontRatio=0.45, tireStiffnessFactor=0.469, minEnableSpeed=-1),
    dbc_dict=dbc_dict('gm_global_a_powertrain_volt', 'gm_global_a_object', chassis_dbc='gm_global_a_chassis')
  )
  CADILLAC_ATS = GMPlatformConfig(
    "CADILLAC ATS Premium Performance 2018",
    [GMCarDocs("Cadillac ATS Premium Performance 2018")],
    GMCarSpecs(mass=1601, wheelbase=2.78, steerRatio=15.3),
  )
  MALIBU = GMPlatformConfig(
    "CHEVROLET MALIBU PREMIER 2017",
    [GMCarDocs("Chevrolet Malibu Premier 2017")],
    GMCarSpecs(mass=1496, wheelbase=2.83, steerRatio=15.8, centerToFrontRatio=0.4),
  )
  ACADIA = GMPlatformConfig(
    "GMC ACADIA DENALI 2018",
    [GMCarDocs("GMC Acadia 2018", video_link="https://www.youtube.com/watch?v=0ZN6DdsBUZo")],
    GMCarSpecs(mass=1975, wheelbase=2.86, steerRatio=14.4, centerToFrontRatio=0.4),
  )
  BUICK_LACROSSE = GMPlatformConfig(
    "BUICK LACROSSE 2017",
    [GMCarDocs("Buick LaCrosse 2017-19", "Driver Confidence Package 2")],
    GMCarSpecs(mass=1712, wheelbase=2.91, steerRatio=15.8, centerToFrontRatio=0.4),
  )
  BUICK_REGAL = GMPlatformConfig(
    "BUICK REGAL ESSENCE 2018",
    [GMCarDocs("Buick Regal Essence 2018")],
    GMCarSpecs(mass=1714, wheelbase=2.83, steerRatio=14.4, centerToFrontRatio=0.4),
  )
  ESCALADE = GMPlatformConfig(
    "CADILLAC ESCALADE 2017",
    [GMCarDocs("Cadillac Escalade 2017", "Driver Assist Package")],
    GMCarSpecs(mass=2564, wheelbase=2.95, steerRatio=17.3),
  )
  ESCALADE_ESV = GMPlatformConfig(
    "CADILLAC ESCALADE ESV 2016",
    [GMCarDocs("Cadillac Escalade ESV 2016", "Adaptive Cruise Control (ACC) & LKAS")],
    GMCarSpecs(mass=2739, wheelbase=3.302, steerRatio=17.3, tireStiffnessFactor=1.0),
  )
  ESCALADE_ESV_2019 = GMPlatformConfig(
    "CADILLAC ESCALADE ESV 2019",
    [GMCarDocs("Cadillac Escalade ESV 2019", "Adaptive Cruise Control (ACC) & LKAS")],
    ESCALADE_ESV.specs,
  )
  BOLT_EUV = GMPlatformConfig(
    "CHEVROLET BOLT EUV 2022",
    [
      GMCarDocs("Chevrolet Bolt EUV 2022-23", "Premier or Premier Redline Trim without Super Cruise Package", video_link="https://youtu.be/xvwzGMUA210"),
      GMCarDocs("Chevrolet Bolt EV 2022-23", "2LT Trim with Adaptive Cruise Control Package"),
    ],
    GMCarSpecs(mass=1669, wheelbase=2.63779, steerRatio=16.8, centerToFrontRatio=0.4, tireStiffnessFactor=1.0),
  )
  SILVERADO = GMPlatformConfig(
    "CHEVROLET SILVERADO 1500 2020",
    [
      GMCarDocs("Chevrolet Silverado 1500 2020-21", "Safety Package II"),
      GMCarDocs("GMC Sierra 1500 2020-21", "Driver Alert Package II", video_link="https://youtu.be/5HbNoBLzRwE"),
    ],
    GMCarSpecs(mass=2450, wheelbase=3.75, steerRatio=16.3, tireStiffnessFactor=1.0),
  )
  EQUINOX = GMPlatformConfig(
    "CHEVROLET EQUINOX 2019",
    [GMCarDocs("Chevrolet Equinox 2019-22")],
    GMCarSpecs(mass=1588, wheelbase=2.72, steerRatio=14.4, centerToFrontRatio=0.4),
  )
  TRAILBLAZER = GMPlatformConfig(
    "CHEVROLET TRAILBLAZER 2021",
    [GMCarDocs("Chevrolet Trailblazer 2021-22")],
    GMCarSpecs(mass=1345, wheelbase=2.64, steerRatio=16.8, centerToFrontRatio=0.4, tireStiffnessFactor=1.0),
  )
  # Separate car def is required when there is no ASCM
  # (for now) unless there is a way to detect it when it has been unplugged...
  VOLT_CC = GMPlatformConfig(
    "CHEVROLET VOLT NO ACC",
    [GMCarDocs("Chevrolet Volt 2017-18")],
    GMCarSpecs(mass=1607, wheelbase=2.69, steerRatio=17.7, centerToFrontRatio=0.45, tireStiffnessFactor=1.0),
  )
  BOLT_CC = GMPlatformConfig(
    "CHEVROLET BOLT EV NO ACC",
    [GMCarDocs("Chevrolet Bolt No ACC")],
    GMCarSpecs(mass=1669, wheelbase=2.63779, steerRatio=16.8, centerToFrontRatio=0.4, tireStiffnessFactor=1.0),
  )
  EQUINOX_CC = GMPlatformConfig(
    "CHEVROLET EQUINOX NO ACC",
    [GMCarDocs("Chevrolet Equinox No ACC")],
    GMCarSpecs(mass=3500, wheelbase=2.72, steerRatio=14.4, centerToFrontRatio=0.4, tireStiffnessFactor=1.0),
  )
  SUBURBAN = GMPlatformConfig(
    "CHEVROLET SUBURBAN PREMIER 2016",
    [GMCarDocs("Chevrolet Suburban Premier 2016-2020")],
    CarSpecs(mass=2731, wheelbase=3.302, steerRatio=17.3, centerToFrontRatio=0.49),
  )
  SUBURBAN_CC = GMPlatformConfig(
    "CHEVROLET SUBURBAN NO ACC",
    [GMCarDocs("Chevrolet Suburban No ACC")],
    GMCarSpecs(mass=2731, wheelbase=3.032, steerRatio=17.3, centerToFrontRatio=0.49, tireStiffnessFactor=1.0),
  )
  YUKON_CC = GMPlatformConfig(
    "GMC YUKON NO ACC",
    [GMCarDocs("GMC Yukon No ACC")],
    CarSpecs(mass=2541, wheelbase=2.95, steerRatio=16.3, centerToFrontRatio=0.4),
  )
  CT6_CC = GMPlatformConfig(
    "CADILLAC CT6 NO ACC",
    [GMCarDocs("Cadillac CT6 No ACC")],
    CarSpecs(mass=2358, wheelbase=3.11, steerRatio=17.7, centerToFrontRatio=0.4),
  )
  TRAILBLAZER_CC = GMPlatformConfig(
    "CHEVROLET TRAILBLAZER NO ACC",
    [GMCarDocs("Chevrolet Trailblazer 2024 No ACC")],
    GMCarSpecs(mass=1345, wheelbase=2.64, steerRatio=16.8, centerToFrontRatio=0.4, tireStiffnessFactor=1.0),
  )
  XT4 = GMPlatformConfig(
    "CADILLAC XT4 2023",
    [GMCarDocs("Cadillac XT4 2023", "Driver Assist Package")],
    CarSpecs(mass=1660, wheelbase=2.78, steerRatio=14.4, centerToFrontRatio=0.4),
  )
  BABYENCLAVE = GMPlatformConfig(
    "BUICK BABY ENCLAVE 2020",
    [GMCarDocs("Buick Baby Enclave 2020-23")],
    CarSpecs(mass=2050, wheelbase=2.86, steerRatio=16.0, centerToFrontRatio=0.5),
  )
  TRAX = GMPlatformConfig(
    "CHEVROLET TRAX 2024",
    [GMCarDocs("Chevrolet TRAX 2024")],
    CarSpecs(mass=1365, wheelbase=2.7, steerRatio=16.4, centerToFrontRatio=0.4),
  )


class CruiseButtons:
  INIT = 0
  UNPRESS = 1
  RES_ACCEL = 2
  DECEL_SET = 3
  MAIN = 5
  CANCEL = 6

class AccState:
  OFF = 0
  ACTIVE = 1
  FAULTED = 3
  STANDSTILL = 4

class CanBus:
  POWERTRAIN = 0
  OBSTACLE = 1
  CAMERA = 2
  CHASSIS = 2
  LOOPBACK = 128
  DROPPED = 192

class GMFlags(IntFlag):
  PEDAL_LONG = 1
  CC_LONG = 2
  NO_CAMERA = 4
  NO_ACCELERATOR_POS_MSG = 8

# In a Data Module, an identifier is a string used to recognize an object,
# either by itself or together with the identifiers of parent objects.
# Each returns a 4 byte hex representation of the decimal part number. `b"\x02\x8c\xf0'"` -> 42790951
GM_BOOT_SOFTWARE_PART_NUMER_REQUEST = b'\x1a\xc0'  # likely does not contain anything useful
GM_SOFTWARE_MODULE_1_REQUEST = b'\x1a\xc1'
GM_SOFTWARE_MODULE_2_REQUEST = b'\x1a\xc2'
GM_SOFTWARE_MODULE_3_REQUEST = b'\x1a\xc3'

# Part number of XML data file that is used to configure ECU
GM_XML_DATA_FILE_PART_NUMBER = b'\x1a\x9c'
GM_XML_CONFIG_COMPAT_ID = b'\x1a\x9b'  # used to know if XML file is compatible with the ECU software/hardware

# This DID is for identifying the part number that reflects the mix of hardware,
# software, and calibrations in the ECU when it first arrives at the vehicle assembly plant.
# If there's an Alpha Code, it's associated with this part number and stored in the DID $DB.
GM_END_MODEL_PART_NUMBER_REQUEST = b'\x1a\xcb'
GM_END_MODEL_PART_NUMBER_ALPHA_CODE_REQUEST = b'\x1a\xdb'
GM_BASE_MODEL_PART_NUMBER_REQUEST = b'\x1a\xcc'
GM_BASE_MODEL_PART_NUMBER_ALPHA_CODE_REQUEST = b'\x1a\xdc'
GM_FW_RESPONSE = b'\x5a'

GM_FW_REQUESTS = [
  GM_BOOT_SOFTWARE_PART_NUMER_REQUEST,
  GM_SOFTWARE_MODULE_1_REQUEST,
  GM_SOFTWARE_MODULE_2_REQUEST,
  GM_SOFTWARE_MODULE_3_REQUEST,
  GM_XML_DATA_FILE_PART_NUMBER,
  GM_XML_CONFIG_COMPAT_ID,
  GM_END_MODEL_PART_NUMBER_REQUEST,
  GM_END_MODEL_PART_NUMBER_ALPHA_CODE_REQUEST,
  GM_BASE_MODEL_PART_NUMBER_REQUEST,
  GM_BASE_MODEL_PART_NUMBER_ALPHA_CODE_REQUEST,
]

GM_RX_OFFSET = 0x400

FW_QUERY_CONFIG = FwQueryConfig(
  requests=[request for req in GM_FW_REQUESTS for request in [
    Request(
      [StdQueries.SHORT_TESTER_PRESENT_REQUEST, req],
      [StdQueries.SHORT_TESTER_PRESENT_RESPONSE, GM_FW_RESPONSE + bytes([req[-1]])],
      rx_offset=GM_RX_OFFSET,
      bus=0,
      logging=True,
    ),
  ]],
  extra_ecus=[(Ecu.fwdCamera, 0x24b, None)],
)

EV_CAR = {CAR.VOLT, CAR.BOLT_EUV, CAR.VOLT_CC, CAR.BOLT_CC}
CC_ONLY_CAR = {CAR.VOLT_CC, CAR.BOLT_CC, CAR.EQUINOX_CC, CAR.SUBURBAN_CC, CAR.YUKON_CC, CAR.CT6_CC, CAR.TRAILBLAZER_CC}

# We're integrated at the Safety Data Gateway Module on these cars
SDGM_CAR = {CAR.XT4, CAR.BABYENCLAVE}

# Slow acceleration cars
SLOW_ACC = {CAR.SILVERADO}

# We're integrated at the camera with VOACC on these cars (instead of ASCM w/ OBD-II harness)
CAMERA_ACC_CAR = {CAR.BOLT_EUV, CAR.SILVERADO, CAR.EQUINOX, CAR.TRAILBLAZER, CAR.TRAX}
CAMERA_ACC_CAR.update({CAR.VOLT_CC, CAR.BOLT_CC, CAR.EQUINOX_CC, CAR.YUKON_CC, CAR.CT6_CC, CAR.TRAILBLAZER_CC})

STEER_THRESHOLD = 1.0

DBC = CAR.create_dbc_map()