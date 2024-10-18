#!/usr/bin/env python3

"""
* Raspi muss durch inverter oder batterie versorgt werden, damit das system
  bereit ist um grosse leistung beim einschalten der hausversorgung zu uebernehmen.
"""
from gi.repository import GLib
import platform
import logging
import sys
import os, time

from traceback import format_exc

sys.path.insert(1, os.path.join(os.path.dirname(__file__), './ext/velib_python'))
from vedbus import VeDbusService
from dbusmonitor import DbusMonitor
# from ve_utils import exit_on_error

onPower =   3000 # watts of rs6000 power when we turn on the slave multiplus, depends on ac current limit of multiplus (19.0A)
OnTimeout = 3600

servicename='com.victronenergy.pvcontrol'

# Manage on/off mode and state of multiplus, rs inverter and rs mpppt
class DeviceControl(object):

    def __init__(self, dbusmonitor, serviceName, controlname, offmode, onmode):
        self.dbusmonitor = dbusmonitor
        self.serviceName = serviceName
        self.controlname = controlname
        self.offmode = offmode
        self.onmode = onmode

        self.devmode = dbusmonitor.get_value(serviceName, controlname)
        logging.info(f"initial mode: {self.serviceName}:{self.controlname}: {self.devmode}")

        self.state = None

    def turnOff(self):
        logging.info(f"DeviceControl: Turn off: {self.serviceName}:{self.controlname}")
        self.dbusmonitor.set_value(self.serviceName, self.controlname, self.offmode)

    def turnOn(self):
        logging.info(f"DeviceControl: Turn on: {self.serviceName}:{self.controlname}")
        self.dbusmonitor.set_value(self.serviceName, self.controlname, self.onmode)

    # def setMode(self, mode):
        # # logging.info('update mp2 mode: %s' % mode)
        # logging.info(f"update mode: {self.serviceName}:{self.controlname}: {self.devmode}")
        # self.devmode = mode

    def isOn(self):
        return self.devmode == self.onmode

    def watch(self, service, path, value):
        # logging.info(f"watch: {self.serviceName} {service}: {path}")

        if service == self.serviceName:
            if path == self.controlname:
                logging.info(f"{self.serviceName}:{path}: set mode to:: {value}")
                self.devmode = value
            elif path == "/State":
                logging.info(f"{self.serviceName}:{path}: set state to:: {value}")
                self.state = value

    def getState(self):
        return self.state

def my_exit_on_error(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except:
        # try:
        logging.info ('my_exit_on_error: there was an exception. Printing stacktrace will be tried and then exit')
        logging.info(format_exc())
        # except:
            # pass

        # sys.exit() is not used, since that throws an exception, which does not lead to a program
        # halt when used in a dbus callback, see connection.py in the Python/Dbus libraries, line 230.
        os._exit(1)

class PVControl(object):

    def __init__(self, productname='IBR PV Control', connection='pvcontrol'):

        logging.debug("Service %s starting... "% servicename)

        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        dbus_tree= {
                # rs 6000
                'com.victronenergy.inverter': { '/Mode': dummy, '/State': dummy, '/Ac/Out/L1/P': dummy }  ,
                # Multiplus 8000
                'com.victronenergy.vebus': { '/Mode': dummy, '/Ac/Out/L1/P': dummy, "/State": dummy},
                # watch cell voltages
                # 'com.victronenergy.battery': {
                    # "/System/MaxCellVoltage": dummy,
                    # "/System/MaxVoltageCellId": dummy,
                    # "/System/MinCellVoltage": dummy,
                    # "/System/MinVoltageCellId": dummy,
                    # },
                # read batt. voltage and control charging voltage
                # 'com.victronenergy.solarcharger': { '/Link/ChargeVoltage': dummy, "/Dc/0/Voltage": dummy},
                'com.victronenergy.system': { '/Dc/Battery/TimeToGo': dummy},
                }

        self._dbusmonitor = DbusMonitor(dbus_tree, valueChangedCallback=self.value_changed_wrapper)

        # Get dynamic servicename for rs6 (ve.can)
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.inverter')
        if not serviceList:
            # Restart process
            logging.info("service com.victronenergy.inverter not registered yet, exiting...")
            sys.exit(0)
        self.vecan_service = serviceList[0]
        logging.info("service of inverter rs6: " +  self.vecan_service)

    	# Get dynamic servicename for mp2 (ve.bus)
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.vebus')
        if not serviceList:
            # Restart process
            logging.info("service com.victronenergy.vebus not registered yet, exiting...")
            sys.exit(0)
        self.vebus_service = serviceList[0]
        logging.info("service of mp2: " + self.vebus_service)

        self._dbusservice = VeDbusService(servicename)

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', 1) # deviceinstance)
        self._dbusservice.add_path('/ProductId', 0)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/FirmwareVersion', 0)
        self._dbusservice.add_path('/HardwareVersion', 0)
        self._dbusservice.add_path('/Connected', 1)

        self._dbusservice.add_path('/A/P', 1)
        self._dbusservice.add_path('/A/Timer', 1)
        self._dbusservice.add_path('/A/MaxPMp', 1)
        self._dbusservice.add_path('/A/MaxPRs', 1)
        self._dbusservice.add_path('/A/MaxPon', 1)

        self._dbusservice['/A/P'] = 0
        self._dbusservice['/A/Timer'] = 0
        self._dbusservice['/A/MaxPMp'] = 0
        self._dbusservice['/A/MaxPRs'] = 0
        self._dbusservice['/A/MaxPon'] = 0

        # read initial value of rs6000 output power
        self.watt = self._dbusmonitor.get_value(self.vecan_service, "/Ac/Out/L1/P") or 0
        logging.info('initial rs6 watts: %d' % self.watt)
        invmode = self._dbusmonitor.get_value(self.vecan_service, "/Mode")
        logging.info('initial rs6 mode: %d' % invmode)
        if invmode not in range(0, 5): # 0..4
            logging.info(f"unknown rs6000 inverter/mode: {mode}, vecan communication seems dead :-(")
            sys.exit(0)

        # invstate = self._dbusmonitor.get_value(self.vecan_service, "/State")
        # logging.info(f"initial rs6 state: {invstate}")

        timetogo = self._dbusmonitor.get_value("com.victronenergy.system", "/Dc/Battery/TimeToGo")
        logging.info(f'initial system:/Dc/Battery/TimeToGo: {timetogo}')

        # read initial value of mp2 state (modes: https://github.com/victronenergy/venus/wiki/dbus#vebus-systems-multis-quattros-inverters)
        # self.mp2state = self._dbusmonitor.get_value(self.vebus_service, "/Mode")
        # logging.info('initial mp2 mode: %d' % self.mp2state)

        self.mp2Control = DeviceControl(self._dbusmonitor, self.vebus_service, "/Mode", 4, 3)      # multiplus

        # DCL/RS6 hack
        self.rsControl = DeviceControl(self._dbusmonitor, self.vecan_service, "/Mode", 4, 2)      # rs6000

        self.endTimer = 0
        self.maxPon = 0
        self.MaxPMp = 0
        self.MaxPRs = 0

        self.canRestart = 0 # time of last canbus restart

        # GLib.timeout_add(1000, self.update)
        GLib.timeout_add(1000, my_exit_on_error, self.update)

    def update(self):

        # Hack, check inverter, avoid "no inverters" error from systemcalc
        mode = self._dbusmonitor.get_value(self.vecan_service, "/Mode")

        if mode not in range(0, 5): # 0..4
            logging.info(f"inverter/mode: {mode}, vecan communication seems dead :-(")

            if (time.time() - self.canRestart) > 120:
                logging.info(f"trying to restart can0 network interface! ...")
                os.system("ifconfig can0 down; sleep 1; ifconfig can0 up")
                self.canRestart = time.time()
                logging.info(f"ifup/down done...")
            else:
                logging.info(f"restart pending...")

            return True

        # logging.info(f"inverter/mode: {mode}, vecan communication seems ok :-)")
        if self.canRestart:
            logging.info(f"can restart duration: {time.time() - self.canRestart}")
            self.canRestart = 0

        # log maximum power consumption (rs6 + mp2)
        # Note: /Ac/Out/L1/P of multiplus is none if it was never started
        p = self._dbusmonitor.get_value(self.vebus_service, "/Ac/Out/L1/P") or 0
        if p > self.MaxPMp:
            self._dbusservice["/A/MaxPMp"] = p
            self.MaxPMp = p

            # State 8: passthrough, state 9: inverting, state 10: assisting
            # if self._dbusmonitor.get_value(self.vebus_service, "/State") == 10:
            if self.mp2Control.getState() == 10:
                self._dbusservice["/A/MaxPRs"] = self.watt

        # test timer timeout and switch off multiplus
        dt = self.endTimer - time.time()
        # inverterState = self._dbusmonitor.get_value(self.vecan_service, "/State")

        # if dt < 0 and self.mp2state != 4:
        # if self.mp2Control.isOn() and ((inverterState != 9) or (dt < 0)):
        if self.mp2Control.isOn() and (dt < 0):
            # switch off mp2
            # logging.info(f"stopping mp2, inverterState: {inverterState}, dt: {dt}...")
            logging.info(f"stopping mp2, dt: {dt}...")
            # self._dbusmonitor.set_value(self.vebus_service, "/Mode", 4)
            self.mp2Control.turnOff()

        self._dbusservice["/A/P"] = self.watt
        if dt > 0:
            self._dbusservice["/A/Timer"] = int(dt)
        else:
            self._dbusservice["/A/Timer"] = 0

        return True

    # Calls value_changed with exception handling
    def value_changed_wrapper(self, *args, **kwargs):
        # self.value_changed(*args, **kwargs)
        my_exit_on_error(self.value_changed, *args, **kwargs)

    def value_changed(self, service, path, options, changes, deviceInstance):
        # logging.info('value_changed %s %s %s' % (service, path, str(changes)))

        self.mp2Control.watch(service, path, changes["Value"])
        self.rsControl.watch(service, path, changes["Value"])

        # if path == "/Mode":

            # self.mp2state = changes["Value"]
            # logging.info('update mp2 mode: %s' % self.mp2state)
            # mp2state = changes["Value"]
            # self.mp2Control.setState(mp2state)

        if service == self.vecan_service:

            if path == "/Ac/Out/L1/P":

                self.watt = changes["Value"]
                # logging.info('update watt: %d' % self.watt)

                if self.watt > onPower:
                    # if self.mp2state != 3:
                    if not self.mp2Control.isOn():
                        logging.info("starting mp2..., watt: %d" % self.watt)
                        # self._dbusmonitor.set_value(self.vebus_service, "/Mode", 3)
                        self.mp2Control.turnOn()
                        if self.watt > self.maxPon:
                            self.maxPon = self.watt
                            self._dbusservice["/A/MaxPon"] = self.watt
                    self.endTimer = time.time() + OnTimeout

        elif service == "com.victronenergy.system":

            # RS6000 DCL hack:
            # It is not enough to set DCL to zero to turn off the rs6000, it turns on for short amounts of time
            # every 2 minutes...
            # Therefore we turn it of hard here using its /Mode dbus reg.
            if path == "/Dc/Battery/TimeToGo":

                timetogo = changes["Value"]
                logging.info(f'system:/Dc/Battery/TimeToGo changed to: {timetogo}')

                if timetogo:
                    if not self.rsControl.isOn():
                        self.rsControl.turnOn()
                else:
                    if self.rsControl.isOn():
                        self.rsControl.turnOff()

    # returns a tuple (servicename, instance)
    def _get_service_having_lowest_instance(self, classfilter=None): 
        services = self._get_connected_service_list(classfilter=classfilter)
        if len(services) == 0: return None
        s = sorted((value, key) for (key, value) in services.items())
        return (s[0][1], s[0][0])

    def _get_connected_service_list(self, classfilter=None):
        services = self._dbusmonitor.get_service_list(classfilter=classfilter)
        # self._remove_unconnected_services(services)
        return services

# === All code below is to simply run it from the commandline for debugging purposes ===

# It will created a dbus service called com.victronenergy.pvinverter.output.
# To try this on commandline, start this program in one terminal, and try these commands
# from another terminal:
# dbus com.victronenergy.pvinverter.output
# dbus com.victronenergy.pvinverter.output /Ac/Energy/Forward GetValue
# dbus com.victronenergy.pvinverter.output /Ac/Energy/Forward SetValue %20
#
# Above examples use this dbus client: http://code.google.com/p/dbus-tools/wiki/DBusCli
# See their manual to explain the % in %20

def main():

    # set timezone used for log entries
    # os.environ['TZ'] = 'Europe/Berlin'
    # time.tzset()

    format = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
    logging.basicConfig(level=logging.DEBUG, format=format, datefmt="%d.%m.%y_%X_%Z")

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    pvControl = PVControl( )

    logging.info('Connected to dbus, and switching over to GLib.MainLoop() (= event based)')
    mainloop = GLib.MainLoop()

    mainloop.run()


if __name__ == "__main__":
    main()


