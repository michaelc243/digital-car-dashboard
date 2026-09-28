import obd
import time
import random
import bisect
import logging
from threading import Event, Thread
from flask import Flask, jsonify, render_template
from flask_socketio import SocketIO

#obd.logger.setLevel(obd.logging.DEBUG)

POLL_RATE = 50    # milliseconds
UPDATE_RATE = 50

ENGINE_DISPLACEMENT = 3.5   # liters
R_SPECIFIC = 287.05 # Gas constant for air
GAS_DENSITY_GRAMS_PER_GALLON = 2839

RPM_AXIS = [800, 2000, 3500, 5000, 6000]
MAP_AXIS = [30, 50, 80, 100]
# Each row corresponds to an RPM point; each value in the row is a MAP point
VE_GRID = [
    [0.45, 0.55, 0.65, 0.75], # 800 RPM
    [0.55, 0.70, 0.80, 0.85], # 2000 RPM
    [0.60, 0.75, 0.85, 0.90], # 3500 RPM
    [0.65, 0.80, 0.90, 0.95], # 5000 RPM
    [0.60, 0.75, 0.85, 0.90]  # 6000 RPM
]

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

ip = "192.168.0.10"
port = 35000

#connection = obd.Async(ip, port)
#connection.watch(obd.commands.SPEED)
#connection.watch(obd.commands.RPM)
#connection.watch(obd.commands.INTAKE_TEMP)
#connection.watch(obd.commands.INTAKE_PRESSURE)
#connection.watch(obd.commands.COMMANDED_EQUIV_RATIO)
#connection.start()

connection = obd.OBD(ip, port)

def query():
    global AFR
    
    speed = connection.query(obd.commands.SPEED)
    if not speed.is_null(): 
        speed = speed.value.to("mph").magnitude
    else:
        print("Error: Speed is null")

    rpm = connection.query(obd.commands.RPM)
    if not rpm.is_null(): 
        rpm = rpm.value.to("rpm").magnitude
        print(rpm)
    else:
        print("Error: RPM is null")

    MAF = connection.query(obd.commands.MAF)
    MAF = MAF.value.to("g/s").magnitude

    CER = connection.query(obd.commands.COMMANDED_EQUIV_RATIO)
    CER = CER.value.magnitude        				# Commanded Air-Fuel Equivalence Ratio (lambda)

    currentAFR = 14.7 * CER
    #volume_per_sec = (rpm / 60 / 2) * (ENGINE_DISPLACEMENT / 1000)
    #maf = (intakePressure * 1000 * volume_per_sec * VE) / (R_SPECIFIC * intakeTemp) * 1000

    # Calculate Fuel Flow
    gallonsPerHour = ((MAF / currentAFR) * 3600) / GAS_DENSITY_GRAMS_PER_GALLON

    #print(f"VE = {VE}")

    return {
        "vehicleSpeed": speed,
        "gallonsPerHour": gallonsPerHour,
        "rpm": rpm
    }

def getVE(rpm, map_kpa):
    """Linearly interpolates Volumetric Efficiency based on current RPM and MAP."""
    # Clamp values to map limits to avoid index errors
    rpm = max(min(rpm, RPM_AXIS[-1]), RPM_AXIS[0])
    map_kpa = max(min(map_kpa, MAP_AXIS[-1]), MAP_AXIS[0])

    # Find the indices of the bounding points
    r_idx = bisect.bisect_right(RPM_AXIS, rpm) - 1
    m_idx = bisect.bisect_right(MAP_AXIS, map_kpa) - 1
    
    # Handle exact match on the top end
    if r_idx < 0: r_idx = 0
    if m_idx < 0: m_idx = 0

    # Get the 4 corners for interpolation
    x1, x2 = RPM_AXIS[r_idx], RPM_AXIS[r_idx+1]
    y1, y2 = MAP_AXIS[m_idx], MAP_AXIS[m_idx+1]
    
    q11 = VE_GRID[r_idx][m_idx]     # Bottom Left
    q12 = VE_GRID[r_idx][m_idx+1]   # Top Left
    q21 = VE_GRID[r_idx+1][m_idx]   # Bottom Right
    q22 = VE_GRID[r_idx+1][m_idx+1] # Top Right

    # Bilinear Interpolation Formula
    ve = (q11 * (x2 - rpm) * (y2 - map_kpa) +
          q21 * (rpm - x1) * (y2 - map_kpa) +
          q12 * (x2 - rpm) * (map_kpa - y1) +
          q22 * (rpm - x1) * (map_kpa - y1)) / ((x2 - x1) * (y2 - y1))
    
    return ve


def getAverageMPG(duration=5, interval=0.5):
    print(f"Getting average MPG... for ({duration} seconds)")
    totalMiles = 0.0
    totalGallons = 0.0
    
    start = time.time()
    last = start

    while(time.time() - start < duration):
        now = time.time()
        delta = now - last
        last = now

        ecuData = query()

        gallonsUsed = ecuData["gallonsPerHour"] * (delta / 3600)
        milesTraveled = ecuData["vehicleSpeed"] * (delta / 3600)

        totalMiles += milesTraveled
        totalGallons += gallonsUsed

    return totalMiles / totalGallons


def poll():
    global POLL_RATE
    global UPDATE_RATE

    lastWebSendTime = 0

    totalMiles = 0.0
    totalGallons = 0.0

    last = time.time()
    while not condition.is_set():
        now = time.time()
        delta = now - last
        last = now

        ecuData = query()
        
        hoursSinceLastUpdate = delta / 3600

        gallonsUsed = ecuData["gallonsPerHour"] * hoursSinceLastUpdate
        milesTraveled = ecuData["vehicleSpeed"] * hoursSinceLastUpdate

        totalMiles += milesTraveled
        totalGallons += gallonsUsed

        if (now - lastWebSendTime) > UPDATE_RATE / 1000:
            lastWebSendTime = now
            try: avgMPG = totalMiles / totalGallons
            except ZeroDivisionError: avgMPG = 0
            
            packet = {
                "r": ecuData["rpm"],
                "s": ecuData["vehicleSpeed"],
                "i": ecuData["vehicleSpeed"] / ecuData["gallonsPerHour"] if ecuData["gallonsPerHour"] > 0.01 else 0,
                "a": avgMPG,
                "m": totalMiles
            }

            print(packet)

            socketio.emit('obd_update', packet)

            socketio.sleep(POLL_RATE / 1000)

    zero_packet = {
        "r": 0,
        "s": 0,
        "i": 0,
        "a": 0,
        "m": 0
    }
 

    socketio.emit('obd_update', zero_packet)


def getInstantMPG(ecuData):
    return ecuData["vehicleSpeed"] / ecuData["gallonsPerHour"]

    # fuelRate = maf / 14.7           # Divide mass air flow by combustion air/fuel ratio to get fuel rate in grams/sec
    # fuelRate = fuelRate / 745       # Divide fuel rate in grams/sec by 745 to get fuel rate in liters/sec
    # fuelRate = fuelRate / 3.785     # Divide fuel rate in liters/sec to get fuel rate in gallons/sec
    # fuelRate = fuelRate * 60 * 60   # Multiply fuel rate in gallons/sec by 3600 to get fuel rate in gallons/hr


@app.route("/")
def index():
    return render_template("index.html")

condition = Event()

if __name__ == "__main__":
    thread = Thread(target=poll)
    thread.start()

    socketio.run(app, host='127.0.0.1', port=5001, debug=False, allow_unsafe_werkzeug=True)
    print("Shutting down thread...")
    condition.set() # tells thread to end
