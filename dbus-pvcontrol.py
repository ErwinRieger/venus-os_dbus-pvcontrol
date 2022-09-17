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

onPower =   2750 # watts of rs6000 power when we turn on slave the multiplus, depends on ac current limit of multiplus
OnTimeout = 1800

servicename='com.victronenergy.pvcontrol'
# servicename='de.ibrieger.pvcontrol' # xxx does not work with dbus-spy...

class PVControl(object):

    def __init__(self, productname='IBR PV Control', connection='pvcontrol'):

        logging.debug("Service %s starting... "% servicename)

        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        dbus_tree= {
                'com.victronenergy.inverter': { '/Ac/Out/L1/P': dummy }  ,
                'com.victronenergy.vebus': { '/Mode': dummy, '/Ac/Out/L1/P': dummy},
                }

        self._dbusmonitor = DbusMonitor(dbus_tree, valueChangedCallback=self._value_changed)

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
        self.watt = self._dbusmonitor.get_value(self.vecan_service, "/Ac/Out/L1/P")
        logging.info('initial rs6 watts: %d' % self.watt)

        # read initial value of mp2 state (modes: https://github.com/victronenergy/venus/wiki/dbus#vebus-systems-multis-quattros-inverters)
        self.mp2state = self._dbusmonitor.get_value(self.vebus_service, "/Mode")
        logging.info('initial mp2 mode: %d' % self.mp2state)
 
        self.endTimer = 0
        self.maxPon = 0
        self.MaxPMp = 0
        self.MaxPRs = 0

        # GLib.timeout_add(10000, self._update)
        GLib.timeout_add(10000, exit_on_error, self._update)

    def _update(self):

        # log maximum power consumption (rs6 + mp2)
        p = self._dbusmonitor.get_value(self.vebus_service, "/Ac/Out/L1/P")
        if p > self.MaxPMp:
            self._dbusservice["/A/MaxPMp"] = p
            self._dbusservice["/A/MaxPRs"] = self.watt
            self.MaxPMp = p

        # test timer timeout and switch off multiplus
        dt = self.endTimer - time.time()
        if dt < 0 and self.mp2state != 4:
            # switch off mp2
            logging.info("stopping mp2...")
            self._dbusmonitor.set_value(self.vebus_service, "/Mode", 4)

        self._dbusservice["/A/P"] = self.watt
        if dt > 0:
            self._dbusservice["/A/Timer"] = dt
        else:
            self._dbusservice["/A/Timer"] = 0

        return True

    def _value_changed(self, _service, path, value, changes, deviceInstance=None):
        # logging.info('_value_changed %s %s %s' % (_service, path, str(changes)))

        if path == "/Mode":

            self.mp2state = changes["Value"]
            logging.info('update mp2 mode: %s' % self.mp2state)

        elif path == "/Ac/Out/L1/P":

            self.watt = changes["Value"]
            # logging.info('update watt: %d' % self.watt)

            if self.watt > onPower:
                if self.mp2state != 3:
                    logging.info("starting mp2..., watt: %d" % self.watt)
                    self._dbusmonitor.set_value(self.vebus_service, "/Mode", 3)
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
    logging.basicConfig(level=logging.DEBUG)

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    pvControl = PVControl( )

    logging.info('Connected to dbus, and switching over to GLib.MainLoop() (= event based)')
    mainloop = GLib.MainLoop()

    mainloop.run()


if __name__ == "__main__":
    main()


