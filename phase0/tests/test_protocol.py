"""Wire-format tests: SSDP, device descriptor, JSON-RPC transport, Sony shapes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import requests

PHASE0 = Path(__file__).resolve().parents[1]
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

from reticle0 import descriptor, ssdp  # noqa: E402
from reticle0.rpc import RpcClient, RpcResult  # noqa: E402
from reticle0.sony import SonyCamera, match_option, option_list  # noqa: E402

from tests import support  # noqa: E402

DESCRIPTOR_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"
      xmlns:av="urn:schemas-sony-com:av">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:DigitalImagingDevice:1</deviceType>
    <friendlyName>ILCE-7M2</friendlyName>
    <manufacturer>Sony Corporation</manufacturer>
    <modelName>ILCE-7M2</modelName>
    <UDN>uuid:00000000-0000-0000-0000-000000000000</UDN>
    <av:X_ScalarWebAPI_DeviceInfo>
      <av:X_ScalarWebAPI_Version>2.0</av:X_ScalarWebAPI_Version>
      <av:X_ScalarWebAPI_ServiceList>
        <av:X_ScalarWebAPI_Service>
          <av:X_ScalarWebAPI_ServiceType>camera</av:X_ScalarWebAPI_ServiceType>
          <av:X_ScalarWebAPI_ActionList_URL>http://192.168.122.1:10000/sony</av:X_ScalarWebAPI_ActionList_URL>
        </av:X_ScalarWebAPI_Service>
        <av:X_ScalarWebAPI_Service>
          <av:X_ScalarWebAPI_ServiceType>guide</av:X_ScalarWebAPI_ServiceType>
          <av:X_ScalarWebAPI_ActionList_URL>http://192.168.122.1:10000/sony</av:X_ScalarWebAPI_ActionList_URL>
        </av:X_ScalarWebAPI_Service>
      </av:X_ScalarWebAPI_ServiceList>
    </av:X_ScalarWebAPI_DeviceInfo>
  </device>
</root>
"""


class MsearchTests(unittest.TestCase):
    def test_datagram_matches_the_documented_form(self) -> None:
        raw = ssdp.build_msearch(mx=3).decode("ascii")
        self.assertTrue(raw.startswith("M-SEARCH * HTTP/1.1\r\n"))
        self.assertTrue(raw.endswith("\r\n\r\n"))
        self.assertIn("HOST: 239.255.255.250:1900\r\n", raw)
        self.assertIn('MAN: "ssdp:discover"\r\n', raw)
        self.assertIn("MX: 3\r\n", raw)
        self.assertIn(f"ST: {ssdp.SEARCH_TARGET}\r\n", raw)
        self.assertIn("USER-AGENT: ", raw)

    def test_search_target_is_the_scalarwebapi_service(self) -> None:
        self.assertEqual(ssdp.SEARCH_TARGET, "urn:schemas-sony-com:service:ScalarWebAPI:1")


class SsdpResponseTests(unittest.TestCase):
    PAYLOAD = (
        b"HTTP/1.1 200 OK\r\n"
        b"CACHE-CONTROL: max-age=1800\r\n"
        b"EXT:\r\n"
        b"LOCATION: http://192.168.122.1:10000/dd.xml\r\n"
        b"SERVER: Linux/3.14.0 UPnP/1.0 SonyImagingDevice/1.0\r\n"
        b"ST: urn:schemas-sony-com:service:ScalarWebAPI:1\r\n"
        b"USN: uuid:x::urn:schemas-sony-com:service:ScalarWebAPI:1\r\n"
        b"\r\n"
    )

    def test_headers_are_lower_cased_and_trimmed(self) -> None:
        parsed = ssdp.parse_response(self.PAYLOAD, ("192.168.122.1", 1900))
        self.assertEqual(parsed["_status_line"], "HTTP/1.1 200 OK")
        self.assertEqual(parsed["location"], "http://192.168.122.1:10000/dd.xml")
        self.assertEqual(parsed["st"], ssdp.SEARCH_TARGET)
        self.assertEqual(parsed["_source_ip"], "192.168.122.1")
        self.assertEqual(parsed["_source_port"], "1900")

    def test_lf_only_and_trailing_spaces_are_tolerated(self) -> None:
        parsed = ssdp.parse_response(b"HTTP/1.1 200 OK\nLOCATION:   http://cam/dd.xml  \n\n")
        self.assertEqual(parsed["location"], "http://cam/dd.xml")

    def test_bare_lines_without_a_colon_are_skipped(self) -> None:
        parsed = ssdp.parse_response(b"HTTP/1.1 200 OK\r\nEXT:\r\njunk line\r\nLOCATION: http://cam/dd.xml\r\n")
        self.assertEqual(parsed["location"], "http://cam/dd.xml")
        self.assertNotIn("junk line", parsed.values())


class DescriptorTests(unittest.TestCase):
    def test_services_and_camera_endpoint(self) -> None:
        device = descriptor.parse_device_description(DESCRIPTOR_XML, location="http://cam/dd.xml")
        self.assertEqual(device.model_name, "ILCE-7M2")
        self.assertEqual(device.friendly_name, "ILCE-7M2")
        self.assertEqual(device.api_version, "2.0")
        self.assertEqual(set(device.services), {"camera", "guide"})
        self.assertEqual(device.camera_endpoint, "http://192.168.122.1:10000/sony/camera")

    def test_trailing_slash_in_action_url_is_not_doubled(self) -> None:
        xml = DESCRIPTOR_XML.replace("/sony</av:X_ScalarWebAPI_ActionList_URL>", "/sony/</av:X_ScalarWebAPI_ActionList_URL>")
        device = descriptor.parse_device_description(xml)
        self.assertEqual(device.camera_endpoint, "http://192.168.122.1:10000/sony/camera")

    def test_byte_order_mark_is_stripped(self) -> None:
        device = descriptor.parse_device_description("\ufeff" + DESCRIPTOR_XML)
        self.assertEqual(device.model_name, "ILCE-7M2")

    def test_missing_camera_service_yields_no_endpoint(self) -> None:
        xml = DESCRIPTOR_XML.replace(">camera<", ">system<")
        device = descriptor.parse_device_description(xml)
        self.assertIsNone(device.camera_endpoint)
        self.assertIn("system", device.services)

    def test_as_dict_is_serialisable_and_complete(self) -> None:
        payload = descriptor.parse_device_description(DESCRIPTOR_XML, location="http://cam/dd.xml").as_dict()
        self.assertEqual(
            set(payload),
            {"location", "model_name", "friendly_name", "api_version", "services", "camera_endpoint"},
        )

    def test_malformed_xml_raises(self) -> None:
        with self.assertRaises(Exception):
            descriptor.parse_device_description("<root><device>")


class RpcTransportTests(unittest.TestCase):
    def test_endpoint_is_required(self) -> None:
        with self.assertRaises(ValueError):
            RpcClient("")

    def test_envelope_and_id_sequence(self) -> None:
        session = support.FakeSession()
        client = RpcClient(support.ENDPOINT, session=session)  # type: ignore[arg-type]

        client.call("getVersions")
        client.call("getApplicationInfo", [])
        client.call("getEvent", [True], version="1.3", timeout=70.0)

        first, second, third = session.bodies
        # Sony forbids id 0.
        self.assertEqual([first["id"], second["id"], third["id"]], [1, 2, 3])
        self.assertEqual(first["method"], "getVersions")
        self.assertEqual(first["params"], [])
        self.assertEqual(first["version"], "1.0")
        self.assertEqual(third["version"], "1.3")
        self.assertEqual(session.timeouts[-1], 70.0)

    def test_transport_is_not_retried(self) -> None:
        session = support.FakeSession(support.FakeResponse(body='{"error": [40403, "Still Capturing Not Finished"], "id": 1}'))
        client = RpcClient(support.ENDPOINT, session=session)  # type: ignore[arg-type]
        result = client.call("actTakePicture", [])

        self.assertEqual(len(session.bodies), 1, "a retry would be extra camera traffic")
        self.assertEqual(result.error_code, 40403)
        self.assertFalse(result.ok)
        # 40403 is an application-level answer, not a transport failure.
        self.assertIsNone(result.transport_error)
        self.assertFalse(result.timed_out)

    def test_http_error_still_carries_the_payload(self) -> None:
        session = support.FakeSession(support.FakeResponse(status_code=500, body='{"error": [500, "boom"], "id": 1}'))
        result = RpcClient(support.ENDPOINT, session=session).call("getVersions")  # type: ignore[arg-type]
        self.assertEqual(result.http_status, 500)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 500)

    def test_client_timeout_is_reported_not_raised(self) -> None:
        session = support.FakeSession(requests.exceptions.ReadTimeout("read timeout"))
        result = RpcClient(support.ENDPOINT, session=session).call("getEvent", [True], timeout=5.0)  # type: ignore[arg-type]
        self.assertTrue(result.timed_out)
        self.assertIn("timeout", (result.transport_error or "").lower())
        self.assertFalse(result.ok)

    def test_non_json_body_is_marked_as_such(self) -> None:
        session = support.FakeSession(support.FakeResponse(body="<html>nope</html>"))
        result = RpcClient(support.ENDPOINT, session=session).call("getVersions")  # type: ignore[arg-type]
        self.assertIn("not JSON", result.transport_error or "")
        self.assertFalse(result.ok)

    def test_results_member_is_exposed_separately(self) -> None:
        result = RpcResult(
            method="getMethodTypes",
            endpoint=support.ENDPOINT,
            http_status=200,
            latency_ms=1.0,
            payload={"results": [["getEvent", ["bool"], ["{...}*"], "1.3"]]},
        )
        self.assertIsNone(result.result)
        self.assertEqual(result.results[0][0], "getEvent")


class SonyShapeTests(unittest.TestCase):
    def test_option_list_documented_shape(self) -> None:
        payload = ["1/2", ["30\"", "1\"", "1/2", "1/30"]]
        self.assertEqual(option_list(payload), ["30\"", "1\"", "1/2", "1/30"])

    def test_option_list_bare_shape(self) -> None:
        self.assertEqual(option_list([["a", "b"]]), ["a", "b"])

    def test_option_list_degrades_to_empty(self) -> None:
        for payload in ([], None, "x", [None], ["only-current"]):
            self.assertEqual(option_list(payload), [])

    def test_postview_urls(self) -> None:
        result = support.ok([["http://cam/postview/a.jpg", "http://cam/postview/b.jpg"]])
        self.assertEqual(
            SonyCamera.postview_urls(result),
            ["http://cam/postview/a.jpg", "http://cam/postview/b.jpg"],
        )
        self.assertEqual(SonyCamera.postview_urls(support.ok([])), [])

    def test_match_option_accepts_a_plain_number(self) -> None:
        options = ["1/125", "1\"", "2\"", "20\"", "30\""]
        self.assertEqual(match_option(options, "20"), "20\"")
        self.assertEqual(match_option(options, "2"), "2\"")
        self.assertEqual(match_option(options, "1/125"), "1/125")
        self.assertIsNone(match_option(options, "45"))
        self.assertIsNone(match_option(options, "BULB"))

    def test_available_list(self) -> None:
        self.assertEqual(SonyCamera.available_list(support.ok([["a", "b"]])), ["a", "b"])
        self.assertEqual(SonyCamera.available_list(support.ok("nope")), [])

    def test_version_selection_prefers_the_highest(self) -> None:
        camera = SonyCamera(support.ENDPOINT, client=support.CannedClient({}))
        camera.capabilities.supported = {"getEvent": ["1.0", "1.3", "1.1"]}
        self.assertEqual(camera.capabilities.version_of("getEvent"), "1.3")
        self.assertEqual(camera.capabilities.version_of("absent", "2.0"), "2.0")

    def test_supported_and_available_are_not_conflated(self) -> None:
        camera = SonyCamera(support.ENDPOINT, client=support.CannedClient({}))
        camera.capabilities.supported = {"startLiveview": ["1.0"]}
        camera.capabilities.available = ["actTakePicture"]
        self.assertTrue(camera.capabilities.supports("startLiveview"))
        self.assertFalse(camera.capabilities.is_available("startLiveview"))


if __name__ == "__main__":
    unittest.main()
