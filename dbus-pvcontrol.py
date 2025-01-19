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
from ve_utils import exit_on_error

MAXPOWER = 6000 # RS6000 inverter
ONPOWER =   MAXPOWER * 0.5 # watts of rs6000 power when we turn on the slave multiplus, depends on ac current limit of multiplus (19.0A)
OFFPOWER = (ONPOWER * 2) / 3 # turn off mp2 when power is below offpower, to add some hysteresis
LOGPOWER = MAXPOWER * 0.8 # log inverter power above this value

OnTimeout = 3600

servicename='com.victronenergy.pvcontrol'

# Manage on/off mode and state of multiplus, rs inverter and rs mpppt
class DeviceControl(object):

    def __init__(self, dbusmonitor, serviceCb, controlname, offmode, onmode):
        self.dbusmonitor = dbusmonitor
        self.serviceCb = serviceCb
        self.controlname = controlname
        self.offmode = offmode
        self.onmode = onmode

        self.devmode = None
        if serviceCb():
            self.devmode = self.dbusmonitor.get_value(serviceCb(), "/Mode")

        logging.info(f"initial mode: {self.serviceCb()}:{self.controlname}: {self.devmode}")
        self.state = None

    def turnOff(self):
        logging.info(f"DeviceControl: Turn off: {self.serviceCb()}:{self.controlname}")
        self.dbusmonitor.set_value(self.serviceCb(), self.controlname, self.offmode)

    def turnOn(self):
        logging.info(f"DeviceControl: Turn on: {self.serviceCb()}:{self.controlname}")
        self.dbusmonitor.set_value(self.serviceCb(), self.controlname, self.onmode)

    def isOn(self):
        return self.devmode == self.onmode

    def isOff(self):
        return self.devmode == self.offmode

    def watch(self, path, value):
        # logging.info(f"watch: {self.serviceCb()}:{path}")

        if path == self.controlname:
            logging.info(f"watch: {self.serviceCb()}:{path}: changed to:: {value}")
            self.devmode = value
        elif path == "/State":
            logging.info(f"watch: {self.serviceCb()}:{path}: changed to:: {value}")
            self.state = value

    def getState(self):
        return self.state

class PVControl(object):

    def __init__(self, productname='IBR PV Control', connection='pvcontrol'):

        logging.debug("Service %s starting... "% servicename)

        self.pvyield = {}

        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        dbus_tree= {
                # inverter rs 6000
                'com.victronenergy.inverter': { '/Mode': dummy, '/State': dummy, '/Ac/Out/L1/P': dummy }  ,
                # inverter multi rs, solarcharger
                'com.victronenergy.multi': { '/Mode': dummy, '/State': dummy, '/Yield/User': dummy }  ,
                # Multiplus 8000
                'com.victronenergy.vebus': { '/Mode': dummy, '/Ac/Out/L1/P': dummy, "/State": dummy},
                # Solar chargers
                'com.victronenergy.solarcharger': { '/Yield/User': dummy},
                'com.victronenergy.system': { '/Dc/Battery/TimeToGo': dummy},
                }

        self._dbusmonitor = DbusMonitor(dbus_tree, valueChangedCallback=self.value_changed_wrapper,
                                        deviceAddedCallback=self.deviceAddedWrapper,
                                        deviceRemovedCallback=self.deviceRemovedWrapper)

        # Get dynamic servicename for rs6 (ve.can)
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.inverter')
        if serviceList:
            vecan_service = serviceList[0]
        else:
            # Restart process
            logging.info("note: service com.victronenergy.inverter not registered yet, exiting...")
            # sys.exit(0)
            vecan_service = None
        logging.info(f"service of inverter rs6: {vecan_service}")

        # Get dynamic servicename for multi-rs
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.multi')
        if serviceList:
            multi_service = serviceList[0]
            self.pvyield[multi_service] = self._dbusmonitor.get_value(multi_service, "/Yield/User") or 0
        else:
            multi_service = None
        logging.info(f"service of multi rs: {multi_service}")

        self.maininverter = multi_service or vecan_service

    	# Get dynamic servicename for mp2 (ve.bus)
        serviceList = self._get_service_having_lowest_instance('com.victronenergy.vebus')
        if serviceList:
            self.vebus_service = serviceList[0]
        else:
            # Restart process
            logging.info("note: service com.victronenergy.vebus not registered yet, exiting...")
            # sys.exit(0)
            self.vebus_service = None
        logging.info(f"service of mp2: {self.vebus_service}")

        pvChargerServiceList = self._dbusmonitor.get_service_list(classfilter="com.victronenergy.solarcharger") or []
        for charger in pvChargerServiceList:
            logging.info(f"pvcharger: {charger}")
            self.pvyield[charger] = self._dbusmonitor.get_value(charger, "/Yield/User") or 0

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
        self._dbusservice.add_path('/TotalPVYield', 1)

        self._dbusservice['/A/P'] = 0
        self._dbusservice['/A/Timer'] = 0
        self._dbusservice['/A/MaxPMp'] = 0
        self._dbusservice['/A/MaxPRs'] = 0
        self._dbusservice['/A/MaxPon'] = 0
        self._dbusservice['/TotalPVYield'] = sum(self.pvyield.values())

        # read initial value of rs6000 (or multi rs) output power
        if self.maininverter:
            self.watt = self._dbusmonitor.get_value(self.maininverter, "/Ac/Out/L1/P") or 0
            logging.info('initial main inverter watts: %d' % self.watt)
            invmode = self._dbusmonitor.get_value(self.maininverter, "/Mode")
            logging.info('initial main inverter mode: %d' % invmode)
            if invmode not in range(0, 5): # 0..4
                logging.info(f"unknown main inverter inverter/mode: {mode}, vecan communication seems dead :-(")
                sys.exit(0)
        else:
            self.watt = 0

        timetogo = self._dbusmonitor.get_value("com.victronenergy.system", "/Dc/Battery/TimeToGo")
        logging.info(f'initial system:/Dc/Battery/TimeToGo: {timetogo}')

        self.mp2Control = DeviceControl(self._dbusmonitor, self.getMultiPlusService, "/Mode", 4, 3)      # multiplus

        # DCL/RS6 hack
        self.rsControl = DeviceControl(self._dbusmonitor, self.getRSService, "/Mode", 1, 3)      # rs6000
        if timetogo != None:
            if timetogo > 0:
                if not self.rsControl.isOn():
                    logging.info("starting invertert ...")
                    self.rsControl.turnOn()
            else:
                if not self.rsControl.isOff():
                    logging.info("stopping invertert ...")
                    self.rsControl.turnOff()

        self.endTimer = 0
        self.maxPon = 0
        self.MaxPMp = 0
        self.MaxPRs = 0

        GLib.timeout_add(1000, exit_on_error, self.update)

    def getRSService(self):
        return self.maininverter

    def getMultiPlusService(self):
        return self.vebus_service

    def update(self):

        # log maximum power consumption (rs6 + mp2)
        # Note: /Ac/Out/L1/P of multiplus is none if it was never started
        p = (self.vebus_service and self._dbusmonitor.get_value(self.vebus_service, "/Ac/Out/L1/P")) or 0
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
        # if self.mp2Control.isOn() and ((inverterState != 9) or (dt < 0)):
        if self.mp2Control.isOn() and (dt < 0):
            # switch off mp2
            logging.info(f"stopping mp2, dt: {dt}...")
            self.mp2Control.turnOff()

        self._dbusservice["/A/P"] = self.watt
        if dt > 0:
            self._dbusservice["/A/Timer"] = int(dt)
        else:
            self._dbusservice["/A/Timer"] = 0

        return True

    # Calls value_changed with exception handling
    def value_changed_wrapper(self, *args, **kwargs):
        exit_on_error(self.value_changed, *args, **kwargs)

    def deviceAddedWrapper(self, *args, **kwargs):
        exit_on_error(self.deviceAddedCallback, *args, **kwargs)

    def deviceRemovedWrapper(self, *args, **kwargs):
        exit_on_error(self.deviceRemovedCallback, *args, **kwargs)

    def deviceAddedCallback(self, service, instance):
        logging.info(f"dbus device added: {service}, {type(service)}, {instance}")

        if service.startswith("com.victronenergy.inverter"):
            logging.info(f"main inverter (rs) added...")
            self.maininverter = service
        elif service.startswith("com.victronenergy.multi"):
            logging.info(f"main inverter (multi) added...")
            self.maininverter = service
            self.pvyield[service] = self._dbusmonitor.get_value(service, "/Yield/User") or 0
            self._dbusservice['/TotalPVYield'] = sum(self.pvyield.values())
        elif service.startswith("com.victronenergy.vebus"):
            logging.info(f"multiplus added...")
            self.vebus_service = service
        elif service.startswith("com.victronenergy.solarcharger"):
            logging.info(f"solarcharger added...")
            self.pvyield[service] = self._dbusmonitor.get_value(service, "/Yield/User") or 1
            self._dbusservice['/TotalPVYield'] = sum(self.pvyield.values())

    def deviceRemovedCallback(self, service, instance):
        logging.info(f"dbus device removed: {service}, {type(service)}, {instance}")

        if service.startswith("com.victronenergy.inverter") or service.startswith("com.victronenergy.multi"):
            logging.info(f"main inverter removed...")
            self.maininverter = None
        elif service.startswith("com.victronenergy.vebus"):
            logging.info(f"multiplus removed...")
            self.vebus_service = None

    def value_changed(self, service, path, options, changes, deviceInstance):
        # logging.info('value_changed %s %s %s' % (service, path, str(changes)))

        # if path == "/Mode":

            # self.mp2state = changes["Value"]
            # logging.info('update mp2 mode: %s' % self.mp2state)
            # mp2state = changes["Value"]
            # self.mp2Control.setState(mp2state)

        if service == self.maininverter:

            self.rsControl.watch(path, changes["Value"])

            if path == "/Ac/Out/L1/P":

                self.watt = changes["Value"] or 0
                # logging.info('update watt: %d' % self.watt)

                if self.watt > ONPOWER:
                    if self.mp2Control.isOff():
                        logging.info("Starting mp2..., watt: %d" % self.watt)
                        self.mp2Control.turnOn()

                        if self.watt > self.maxPon:
                            self.maxPon = self.watt
                            self._dbusservice["/A/MaxPon"] = self.watt
                    self.endTimer = time.time() + OnTimeout

                    if self.watt > LOGPOWER:
                        logging.info("inverter power: %d" % self.watt)

        elif service == self.vebus_service:

            self.mp2Control.watch(path, changes["Value"])

        elif service == "com.victronenergy.system":

            # RS6000 DCL hack:
            # It is not enough to set DCL to zero to turn off the rs6000, it turns on for short amounts of time
            # every 2 minutes...
            # Therefore we turn it of hard here using its /Mode dbus reg.
            if path == "/Dc/Battery/TimeToGo":

                timetogo = changes["Value"]
                logging.info(f'system:/Dc/Battery/TimeToGo changed to: {timetogo}')

                if timetogo != None:
                    if timetogo > 0:
                        if not self.rsControl.isOn():
                            logging.info("starting inverter")
                            self.rsControl.turnOn()
                    else:
                        if not self.rsControl.isOff():
                            logging.info("stoppng inverter")
                            self.rsControl.turnOff()

        # compute total pv yield
        if path == "/Yield/User":
            # logging.info(f"pvcharger, {service} yield: {changes['Value']}")
            self.pvyield[service] = changes["Value"]
            self._dbusservice["/TotalPVYield"] = sum(self.pvyield.values())

    # returns a tuple (servicename, instance)
    def _get_service_having_lowest_instance(self, classfilter=None): 
        services = self._dbusmonitor.get_service_list(classfilter=classfilter)
        if len(services) == 0: return None
        s = sorted((value, key) for (key, value) in services.items())
        return (s[0][1], s[0][0])

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


