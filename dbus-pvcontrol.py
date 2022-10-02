#!/usr/bin/env python3

"""
* Raspi muss durch inverter oder batterie versorgt werden, damit das system
  bereit ist um grosse leistung beim einschalten der hausversorgung zu uebernehmen.

Nice to have: set MaxPRs only if mp2 is inverting.

"""
from gi.repository import GLib
import platform
import logging
import sys
import os, time

sys.path.insert(1, os.path.join(os.path.dirname(__file__), './ext/velib_python'))
from vedbus import VeDbusService
from dbusmonitor import DbusMonitor
from ve_utils import exit_on_error

# onPower =   2750 # watts of rs6000 power when we turn on slave the multiplus, depends on ac current limit of multiplus (17.5A)
onPower =   3000 # watts of rs6000 power when we turn on slave the multiplus, depends on ac current limit of multiplus (19.0A)
OnTimeout = 1800

servicename='com.victronenergy.pvcontrol'
# servicename='de.ibrieger.pvcontrol' # xxx does not work with dbus-spy...

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
                logging.info(f"set mode to:: {value}")
                self.devmode = value
            elif path == "/State":
                logging.info(f"set state to:: {value}")
                self.state = value

    def getState(self):
        return self.state

class PVControl(object):

    def __init__(self, productname='IBR PV Control', connection='pvcontrol'):

        logging.debug("Service %s starting... "% servicename)

        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        dbus_tree= {
                # rs 6000
                'com.victronenergy.inverter': { '/Mode': dummy, '/Ac/Out/L1/P': dummy, "/State": dummy }  ,
                # Multiplus 8000
                'com.victronenergy.vebus': { '/Mode': dummy, '/Ac/Out/L1/P': dummy, "/State": dummy},
                # watch cell voltages
                'com.victronenergy.battery': {
                    "/System/MaxCellVoltage": dummy,
                    # "/System/MaxVoltageCellId": dummy,
                    "/System/MinCellVoltage": dummy,
                    # "/System/MinVoltageCellId": dummy,
                    },
                # read batt. voltage and control charging voltage
                'com.victronenergy.solarcharger': { '/Link/ChargeVoltage': dummy, "/Dc/0/Voltage": dummy},
                }

        self._dbusmonitor = DbusMonitor(dbus_tree, valueChangedCallback=self.value_changed_wrapper)

	# Get dynamic servicename for rs6 (ve.can)
        # self.vecan_service = self.waitForService('com.victronenergy.inverter')
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.inverter')
        if not serviceList:
            # Restart process
            logging.info("service com.victronenergy.inverter not registered yet, exiting...")
            sys.exit(0)
        self.vecan_service = serviceList[0]
        logging.info("service of inverter rs6: " +  self.vecan_service)

	# Get dynamic servicename for mp2 (ve.bus)
        # self.vebus_service = self.waitForService('com.victronenergy.vebus')
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.vebus')
        if not serviceList:
            # Restart process
            logging.info("service com.victronenergy.vebus not registered yet, exiting...")
            sys.exit(0)
        self.vebus_service = serviceList[0]
        logging.info("service of mp2: " + self.vebus_service)

	# Get dynamic servicename for serial-battery
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.battery')
        if not serviceList:
            # Restart process
            logging.info("service com.victronenergy.battery not registered yet, exiting...")
            sys.exit(0)
        self.batt_service = serviceList[0]
        logging.info("service of battery: " +  self.batt_service)

	# Get dynamic servicename for pv charger
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.solarcharger')
        if not serviceList:
            # Restart process
            logging.info("service com.victronenergy.solarcharger not registered yet, exiting...")
            sys.exit(0)
        self.pv_charger = serviceList[0]
        logging.info("pv service: " +  self.pv_charger)

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

        # read initial value of mp2 state (modes: https://github.com/victronenergy/venus/wiki/dbus#vebus-systems-multis-quattros-inverters)
        # self.mp2state = self._dbusmonitor.get_value(self.vebus_service, "/Mode")
        # logging.info('initial mp2 mode: %d' % self.mp2state)

        self.inverterControl = DeviceControl(self._dbusmonitor, self.vecan_service, "/Mode", 4, 2) # inverter rs 6000
        self.mp2Control = DeviceControl(self._dbusmonitor, self.vebus_service, "/Mode", 4, 3)      # multiplus

        self.endTimer = 0
        self.maxPon = 0
        self.MaxPMp = 0
        self.MaxPRs = 0

        self.packVolt = 55.2 # 3.345 v per cell

        GLib.timeout_add(10000, self.update)
        # GLib.timeout_add(10000, exit_on_error, self.update)

    def update(self):

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
        # if dt < 0 and self.mp2state != 4:
        if dt < 0 and self.mp2Control.isOn():
            # switch off mp2
            logging.info("stopping mp2...")
            # self._dbusmonitor.set_value(self.vebus_service, "/Mode", 4)
            self.mp2Control.turnOff()

        minCellVoltage = self._dbusmonitor.get_value(self.batt_service, "/System/MinCellVoltage")
        logging.info(f"minCellVoltage: {minCellVoltage}")

        # disconnect from battery if a cell voltage is below min voltage
        if minCellVoltage < 3.0 and self.inverterControl.isOn(): # xxx hardcoded
            # turn off inverter
            logging.info(f"turn off inverter, pack voltage: {self._dbusmonitor.get_value(self.pv_charger, '/Dc/0/Voltage')}")
            self.inverterControl.turnOff()

        # re-connect to battery if all cells are above min voltage
        if minCellVoltage > 3.275 and not self.inverterControl.isOn(): # xxx about 50% SOC, hardcoded
            # turn on inverter
            logging.info(f"turn on inverter, pack voltage: {self._dbusmonitor.get_value(self.pv_charger, '/Dc/0/Voltage')}")
            self.inverterControl.turnOn()

        maxCellVoltage = self._dbusmonitor.get_value(self.batt_service, "/System/MaxCellVoltage")
        logging.info(f"maxCellVoltage: {maxCellVoltage}")

        # stop charging if a cell voltage is above 3.45v
        if maxCellVoltage > 3.45: # xxx hardcoded
            # freeze charging voltage
            self.packVolt = self._dbusmonitor.get_value(self.pv_charger, "/Dc/0/Voltage")
            logging.info(f"throttling charger, pack voltage: {self.packVolt}")
            
        # start charging if all cells below 3.4v
        elif maxCellVoltage < 3.40: # xxx hardcoded
            self.packVolt = 55.2 # 3.345 v per cell
            logging.info(f"un-throttling charger, pack voltage: {self._dbusmonitor.get_value(self.pv_charger, '/Dc/0/Voltage')}")

        logging.info(f"setting mppt.ChargeVoltage: {self.packVolt}")
        self._dbusmonitor.set_value(self.pv_charger, "/Link/ChargeVoltage", self.packVolt) # value stays for 60 minutes

        self._dbusservice["/A/P"] = self.watt
        if dt > 0:
            self._dbusservice["/A/Timer"] = int(dt)
        else:
            self._dbusservice["/A/Timer"] = 0

        return True

    # Calls value_changed with exception handling
    def value_changed_wrapper(self, *args, **kwargs):
        exit_on_error(self.value_changed, *args, **kwargs)

    def value_changed(self, service, path, options, changes, deviceInstance):
        # logging.info('value_changed %s %s %s' % (service, path, str(changes)))

        self.inverterControl.watch(service, path, changes["Value"])
        self.mp2Control.watch(service, path, changes["Value"])

        # if path == "/Mode":

            # self.mp2state = changes["Value"]
            # logging.info('update mp2 mode: %s' % self.mp2state)
            # mp2state = changes["Value"]
            # self.mp2Control.setState(mp2state)

        if service == self.vecan_service and path == "/Ac/Out/L1/P":

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

    # Does not work, seems that _dbusmonitor does not re-scan the bus...
    """
    # Wait for and get dynamic servicename for rs6/mp2
    def waitForService(self, sn):
        serviceList = self._get_service_having_lowest_instance(sn)
        while not serviceList:
            logging.info("waiting for service %s..." % sn)
            time.sleep(1)
            serviceList = self._get_service_having_lowest_instance(sn)

        service = serviceList[0]
        logging.info("service for %s: %s" % (sn, service))
        return service
    """

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


