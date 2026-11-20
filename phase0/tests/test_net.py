"""ipconfig parsing and subnet arithmetic.

The captured sample is the real output from the Phase 0 laptop, including the
dual-stack gateway that wraps onto a continuation line. That wrap is exactly
where a naive parser loses the gateway, so it is pinned by a test.
"""

from __future__ import annotations

import ipaddress
import sys
import unittest
from pathlib import Path

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

from reticle0 import net  # noqa: E402

CAPTURED = """

Windows IP Configuration


Wireless LAN adapter Wi-Fi 2:

   Media State . . . . . . . . . . . : Media disconnected
   Connection-specific DNS Suffix  . : 

Wireless LAN adapter Wi-Fi 5:

   Media State . . . . . . . . . . . : Media disconnected
   Connection-specific DNS Suffix  . : 

Wireless LAN adapter Wi-Fi:

   Connection-specific DNS Suffix  . : 
   IPv6 Address. . . . . . . . . . . : 2405:201:5005:e9cc:459f:8662:505b:88ae
   Temporary IPv6 Address. . . . . . : 2405:201:5005:e9cc:7159:5758:ed20:c1fe
   Link-local IPv6 Address . . . . . : fe80::9e6a:2c3:dbb5:f269%10
   IPv4 Address. . . . . . . . . . . : 192.168.29.101
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : fe80::c28f:20ff:fe2a:2061%10
                                       192.168.29.1
"""

LOCALISED = """

Configuración IP de Windows

Adaptador de LAN inalámbrica Wi-Fi:

   Dirección IPv4. . . . . . . . . . : 192.168.122.10
   Máscara de subred . . . . . . . . : 255.255.255.0
   Puerta de enlace predeterminada . : 192.168.122.1

"""


class ParseIpconfigTests(unittest.TestCase):
    def test_captured_output(self) -> None:
        found = net.parse_ipconfig(CAPTURED)
        self.assertEqual(len(found), 1, "disconnected adapters must not appear")
        interface = found[0]
        self.assertEqual(interface.name, "Wireless LAN adapter Wi-Fi")
        self.assertEqual(interface.ipv4, "192.168.29.101")
        self.assertEqual(interface.netmask, "255.255.255.0")
        self.assertEqual(interface.gateway, "192.168.29.1", "gateway wraps onto a continuation line")
        self.assertEqual(str(interface.network), "192.168.29.0/24")
        self.assertEqual(interface.prefix_length, 24)

    def test_localised_field_labels(self) -> None:
        found = net.parse_ipconfig(LOCALISED)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].ipv4, "192.168.122.10")
        self.assertEqual(found[0].netmask, "255.255.255.0")
        self.assertEqual(found[0].gateway, "192.168.122.1")

    def test_empty_valued_field_is_not_an_adapter_header(self) -> None:
        text = (
            "Wireless LAN adapter Wi-Fi:\n"
            "   Connection-specific DNS Suffix  . : \n"
            "   IPv4 Address. . . . . . . . . . . : 10.0.0.5\n"
            "   Subnet Mask . . . . . . . . . . . : 255.255.255.0\n"
        )
        found = net.parse_ipconfig(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "Wireless LAN adapter Wi-Fi")
        self.assertEqual(found[0].ipv4, "10.0.0.5")

    def test_ipv6_only_adapter_is_dropped(self) -> None:
        text = (
            "Ethernet adapter Ethernet:\n"
            "   IPv6 Address. . . . . . . . . . . : fe80::1%4\n"
            "   Default Gateway . . . . . . . . . : fe80::1%4\n"
        )
        self.assertEqual(net.parse_ipconfig(text), [])

    def test_empty_input(self) -> None:
        self.assertEqual(net.parse_ipconfig(""), [])

    def test_as_dict_reports_derived_network(self) -> None:
        payload = net.parse_ipconfig(CAPTURED)[0].as_dict()
        self.assertEqual(payload["network"], "192.168.29.0/24")
        self.assertEqual(payload["prefix_length"], 24)


class SubnetTests(unittest.TestCase):
    def test_host_addresses_excludes_network_and_broadcast(self) -> None:
        hosts = net.host_addresses(ipaddress.IPv4Network("192.168.122.0/30"))
        self.assertEqual(hosts, ["192.168.122.1", "192.168.122.2"])

    def test_oversized_network_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            net.host_addresses(ipaddress.IPv4Network("10.0.0.0/8"))

    def test_camera_direct_network_is_typical_direct_wifi(self) -> None:
        # The A7 II's DIRECT- access point hands out 192.168.122.x/24. Not an
        # assumption the code relies on, but it keeps the arithmetic honest.
        network = ipaddress.IPv4Network("192.168.122.0/24")
        self.assertEqual(len(net.host_addresses(network)), 254)


if __name__ == "__main__":
    unittest.main()
