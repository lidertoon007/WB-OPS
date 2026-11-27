#!/usr/bin/python3
"""Expose only systemd unit status snapshots to the Netdata private bus."""

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib


NAME = "org.freedesktop.systemd1"
PATH = "/org/freedesktop/systemd1"
MANAGER = NAME + ".Manager"
PROPERTIES = "org.freedesktop.DBus.Properties"
ADDRESS = "unix:path=/run/wb-ops-netdata-dbus/system_bus_socket"


class Status(dbus.service.Object):
    def __init__(self, private_bus):
        self.host = dbus.SystemBus().get_object(NAME, PATH)
        self.host_manager = dbus.Interface(self.host, MANAGER)
        self.host_properties = dbus.Interface(self.host, PROPERTIES)
        super().__init__(private_bus, PATH)

    @dbus.service.method(PROPERTIES, in_signature="ss", out_signature="v")
    def Get(self, interface, property_name):
        if interface != MANAGER or property_name != "Version":
            raise dbus.exceptions.DBusException(
                "Only systemd Manager.Version is exposed",
                name="org.freedesktop.DBus.Error.AccessDenied",
            )
        return self.host_properties.Get(interface, property_name)

    @dbus.service.method(MANAGER, in_signature="asas", out_signature="a(ssssssouso)")
    def ListUnitsByPatterns(self, states, patterns):
        # The host method returns metadata and current state; no caller-supplied
        # destination, object path, interface, or method is forwarded.
        return self.host_manager.ListUnitsByPatterns(states, patterns)

    @dbus.service.method(MANAGER, in_signature="", out_signature="a(ssssssouso)")
    def ListUnits(self):
        return self.host_manager.ListUnits()


if __name__ == "__main__":
    DBusGMainLoop(set_as_default=True)
    private_bus = dbus.bus.BusConnection(ADDRESS)
    name = dbus.service.BusName(NAME, bus=private_bus, do_not_queue=True)
    status = Status(private_bus)
    GLib.MainLoop().run()
