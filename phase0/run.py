#!/usr/bin/env python3
"""Reticle Phase 0 command line.

Subcommands map to the investigation phases:

    netinfo    Phase A - local interface baseline (no camera needed)
    discover   Phase B - SSDP discovery + device descriptor -> API endpoint
    probe      Phase C - read-only capability matrix
    events     Phase E - event stream behaviour and camera status transitions
    capture    Phase D - one still, timed, including the 40403 long-exposure path
    set        apply exactly one exposure setting, with read-back
    rec-mode   explicitly issue startRecMode to prepare shooting mode
    sim        run the offline test double

Defaults are conservative: nothing writes to the camera except `set`,
`rec-mode` and `capture`, and live view is never started by any command.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reticle0 import __version__, descriptor, net, ssdp  # noqa: E402
from reticle0.capture import capture_once  # noqa: E402
from reticle0.events import EventStream  # noqa: E402
from reticle0.probe import render_markdown, run_probe  # noqa: E402
from reticle0.recorder import Recorder  # noqa: E402
from reticle0.sony import SonyCamera, match_option  # noqa: E402

BANNER = "Reticle Phase 0 - Sony A7 II (ILCE-7M2) protocol investigation"


def artifact_dir(command: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPO / "artifacts" / f"{stamp}-{command}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def rule(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


# --------------------------------------------------------------------- Phase A

def cmd_netinfo(args: argparse.Namespace) -> int:
    baseline = net.baseline()
    print(BANNER)
    rule("Network baseline")
    print(f"host        : {baseline['hostname']}")
    print(f"primary IPv4: {baseline['primary_ipv4']}")

    for interface in baseline["interfaces"]:
        print(
            f"  {interface['name']:<28} {interface['ipv4']:<16} "
            f"{interface['network']:<20} gw={interface['gateway']}"
        )

    out = artifact_dir("netinfo")
    (out / "baseline.json").write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"\nwritten: {out / 'baseline.json'}")

    if baseline["primary_ipv4"] == "192.168.29.101":
        print("\nNOTE: this is the home LAN. Connect to the camera's DIRECT- network")
        print("      before running `discover`, then re-run `netinfo` to record it.")
    return 0


# --------------------------------------------------------------------- Phase B

def _resolve_endpoint(args: argparse.Namespace, recorder: Recorder) -> str | None:
    if getattr(args, "endpoint", None):
        return str(args.endpoint)

    interfaces = net.interfaces()
    if getattr(args, "interface", None):
        interfaces = [item for item in interfaces if item.ipv4 == args.interface]
        if not interfaces:
            print(f"No local interface with address {args.interface}")
            return None

    rule("SSDP discovery")
    print(f"ST {ssdp.SEARCH_TARGET} -> {ssdp.MCAST_GROUP}:{ssdp.MCAST_PORT}")

    results = []
    for interface in interfaces:
        print(f"  probing via {interface.name} ({interface.ipv4}) ...", flush=True)
        try:
            found = ssdp.discover(interface.ipv4, mx=args.mx)
        except OSError as exc:
            print(f"    socket error: {exc}")
            continue
        for item in found:
            recorder.event("ssdp", source=item.source_ip, location=item.location, server=item.server)
            results.append(item)

    if not results:
        print("\nNo ScalarWebAPI responders.")
        print("Check, in order:")
        print("  1. Camera is in remote-control mode (Smart Remote Control / Ctrl with Smartphone).")
        print("  2. Windows Firewall allows python.exe on Private networks (an inbound UDP reply must return).")
        print("  3. The camera network is the active route for 239.255.255.250.")
        print("  4. If all else fails, re-run with --scan to TCP-sweep the subnet.")
        return None

    endpoint: str | None = None
    for item in results:
        print(f"\n  responder {item.source_ip}")
        print(f"    location: {item.location}")
        try:
            description = descriptor.fetch_device_description(item.location)
        except Exception as exc:  # noqa: BLE001 - surface the real reason
            print(f"    descriptor fetch failed: {exc}")
            continue
        print(f"    model   : {description.model_name}")
        print(f"    friendly: {description.friendly_name}")
        print(f"    api ver : {description.api_version}")
        print(f"    services: {description.services}")
        if description.camera_endpoint:
            print(f"    endpoint: {description.camera_endpoint}")
            endpoint = endpoint or description.camera_endpoint
            recorder.event("descriptor", **description.as_dict())

    return endpoint


def cmd_discover(args: argparse.Namespace) -> int:
    print(BANNER)
    out = artifact_dir("discover")
    recorder = Recorder(out / "trace.jsonl", echo=False)
    endpoint = _resolve_endpoint(args, recorder)

    if not endpoint and args.scan:
        rule("Fallback: TCP sweep")
        interface = net.primary_interface()
        if interface:
            print(f"sweeping {interface.network} for port 80 ...")
            hosts = net.scan_http_hosts(interface.network, exclude={interface.ipv4})
            print(f"responders: {hosts or 'none'}")

    if endpoint:
        print(f"\nRESULT endpoint = {endpoint}")
        (out / "endpoint.txt").write_text(endpoint + "\n", encoding="utf-8")
    print(f"trace: {out / 'trace.jsonl'}")
    return 0 if endpoint else 2


# --------------------------------------------------------------------- Phase C

def _camera_for(args: argparse.Namespace, recorder: Recorder) -> SonyCamera | None:
    endpoint = _resolve_endpoint(args, recorder)
    if not endpoint:
        return None
    cam = SonyCamera(endpoint, recorder=recorder)
    if cam.is_simulated:
        print("\n*** SIMULATED CAMERA - results are not hardware measurements ***")
        recorder.simulated = True
    print(f"\nendpoint: {endpoint}")
    return cam


def cmd_probe(args: argparse.Namespace) -> int:
    print(BANNER)
    out = artifact_dir("probe")
    recorder = Recorder(out / "trace.jsonl")

    camera = _camera_for(args, recorder)
    if camera is None:
        return 2

    rule("Read-only capability probe")
    report = run_probe(camera, recorder)
    if camera.is_simulated:
        report["simulated"] = True

    (out / "capabilities.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    (out / "capability-matrix.md").write_text(markdown, encoding="utf-8")

    if args.publish:
        if report.get("simulated"):
            print("\nNotice: skipped publishing simulated capability matrix to docs/phase0/.")
        else:
            target = REPO / "docs" / "phase0" / "02-capability-matrix.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(markdown, encoding="utf-8")
            print(f"\npublished: {target}")

    derived = report.get("derived", {})
    rule("Summary")
    print(f"application        : {report['identity'].get('application_name')}")
    print(f"API version        : {report['identity'].get('api_version')}")
    print(f"camera status      : {report['protocol'].get('camera_status')}")
    print(f"supported methods  : {report['protocol'].get('supported_method_count')}")
    print(f"available now      : {report['protocol'].get('available_api_count')}")
    print(f"slowest shutter    : {derived.get('shutter', {}).get('slowest')} "
          f"({derived.get('max_exposure_seconds')} s)")
    print(f"bulb via API       : {derived.get('bulb_exposed_by_api')}")
    print(f"awaitTakePicture   : {derived.get('await_take_picture_supported')}")
    print(f"interval still     : supported={derived.get('interval_still_supported')} "
          f"available={derived.get('interval_still_available')}")
    print(f"liveview running   : {derived.get('liveview_running')}")
    print(f"\nreport : {out / 'capabilities.json'}")
    print(f"trace  : {out / 'trace.jsonl'}")
    return 0


# --------------------------------------------------------------------- Phase E

def cmd_events(args: argparse.Namespace) -> int:
    if args.count <= 0:
        print("Error: --count must be a positive integer.")
        return 2
    if args.seconds <= 0:
        print("Error: --seconds must be a positive number.")
        return 2

    print(BANNER)
    out = artifact_dir("events")
    recorder = Recorder(out / "trace.jsonl")

    camera = _camera_for(args, recorder)
    if camera is None:
        return 2

    camera.load_capabilities()
    stream = EventStream(camera, recorder=recorder)

    rule("Snapshot (polling=false)")
    snapshot = stream.snapshot()
    print(json.dumps(snapshot.events, indent=2)[:4000])

    rule(f"Long-poll stream: {args.count} events / {args.seconds}s")
    for index, tick in enumerate(stream.stream(max_events=args.count, seconds=args.seconds), start=1):
        print(f"\n[{index}] {tick.latency_ms:.0f} ms")
        for name, payload in tick.events.items():
            print(f"   {name}: {json.dumps(payload)[:300]}")

    print(f"\ntrace: {out / 'trace.jsonl'}")
    return 0


# --------------------------------------------------------------------- Phase D

def _shutter_target(camera: SonyCamera, requested: str) -> tuple[str | None, list[str]]:
    """Resolve a typed shutter value against the camera's advertised options.

    An operator types ``20`` or ``1/125``; the camera may expect ``20"``.
    Matching against ``getAvailableShutterSpeed`` means the wire value always
    comes from the camera rather than from a guess, and the Windows shell never
    has to carry an embedded double quote.
    """
    options = [str(item) for item in camera.available_options(camera.get_available_shutter_speed())]
    if not options:
        return requested, []
    return match_option(options, requested), options


def cmd_capture(args: argparse.Namespace) -> int:
    if args.budget is not None and args.budget <= 0:
        print("Error: --budget must be a positive number.")
        return 2

    print(BANNER)
    out = artifact_dir("capture")
    recorder = Recorder(out / "trace.jsonl")

    camera = _camera_for(args, recorder)
    if camera is None:
        return 2

    camera.load_capabilities()
    if not camera.capabilities.supports("actTakePicture"):
        print("Camera does not advertise actTakePicture; run `probe` first.")
        return 3

    shutter = args.shutter
    if shutter:
        resolved, options = _shutter_target(camera, shutter)
        if options and resolved is None:
            print(f"shutter {shutter} is not on offer. Available: {options}")
            return 3
        if resolved and resolved != shutter:
            print(f"shutter {shutter} -> {resolved} (camera's advertised form)")
        shutter = resolved

    rule("Single capture")
    result = capture_once(
        camera,
        recorder,
        shutter=shutter,
        set_shutter=bool(shutter),
        budget_seconds=args.budget,
    )

    (out / "capture.json").write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    rule("Result")
    print(f"outcome      : {result.outcome}")
    print(f"shutter      : {result.requested_shutter}")
    print(f"status before: {result.status_before}")
    print(f"status after : {result.status_after}")
    print(f"act latency  : {result.act_latency_ms} ms (error {result.act_error_code})")
    print(f"await calls  : {result.await_calls}")
    print(f"total        : {result.total_ms} ms")
    for note in result.notes:
        print(f"note         : {note}")
    print(f"\ncapture: {out / 'capture.json'}")
    print(f"trace  : {out / 'trace.jsonl'}")
    return 0 if result.outcome == "complete" else 4


# ------------------------------------------------------------------- settings

def cmd_set(args: argparse.Namespace) -> int:
    requested = any(
        value is not None
        for value in (args.shutter, args.iso, args.fnumber, args.exposure_comp)
    )
    if not requested:
        print("Nothing requested. Use --shutter / --iso / --fnumber / --exposure-comp.")
        return 2

    if args.exposure_comp is not None:
        try:
            args.exposure_comp = int(args.exposure_comp)
        except (TypeError, ValueError):
            print(f"Error: --exposure-comp must be an integer index, got {args.exposure_comp}")
            return 2

    print(BANNER)
    out = artifact_dir("set")
    recorder = Recorder(out / "trace.jsonl")

    camera = _camera_for(args, recorder)
    if camera is None:
        return 2

    camera.load_capabilities()

    shutter_options: list[str] = []
    if args.shutter is not None:
        resolved, shutter_options = _shutter_target(camera, args.shutter)
        if shutter_options and resolved is None:
            print(f"shutter {args.shutter} is not on offer. Available: {shutter_options}")
            return 3
        if resolved and resolved != args.shutter:
            print(f"shutter {args.shutter} -> {resolved} (camera's advertised form)")
        args.shutter = resolved

    if args.dry_run:
        # Dry run still reads the option lists: that is the only way to know
        # whether a requested value is legal before sending it.
        for label, value, getter in (
            ("shutter", args.shutter, camera.get_available_shutter_speed),
            ("iso", args.iso, camera.get_available_iso_speed_rate),
            ("fnumber", args.fnumber, camera.get_available_f_number),
        ):
            if value is None:
                continue
            if label == "shutter" and shutter_options:
                options = shutter_options
            else:
                options = [str(item) for item in camera.available_options(getter())]
            print(f"would set {label}={value}; available ({len(options)}): {options}")
        if args.exposure_comp is not None:
            print(f"would set exposure_compensation={args.exposure_comp}")
        print(f"trace: {out / 'trace.jsonl'}")
        return 0

    applied: list[str] = []
    failed: list[str] = []

    if args.shutter is not None:
        result = camera.set_shutter_speed(args.shutter)
        print(f"setShutterSpeed({args.shutter}) -> {result.error or 'ok'}")
        if result.ok:
            applied.append(f"shutter={_read_back(camera.get_shutter_speed())}")
        else:
            failed.append(f"shutter ({result.error})")

    if args.iso is not None:
        result = camera.set_iso_speed_rate(args.iso)
        print(f"setIsoSpeedRate({args.iso}) -> {result.error or 'ok'}")
        if result.ok:
            applied.append(f"iso={_read_back(camera.get_iso_speed_rate())}")
        else:
            failed.append(f"iso ({result.error})")

    if args.fnumber is not None:
        result = camera.set_f_number(args.fnumber)
        print(f"setFNumber({args.fnumber}) -> {result.error or 'ok'}")
        if result.ok:
            applied.append(f"fnumber={_read_back(camera.get_f_number())}")
        else:
            failed.append(f"fnumber ({result.error})")

    if args.exposure_comp is not None:
        result = camera.set_exposure_compensation(args.exposure_comp)
        print(f"setExposureCompensation({args.exposure_comp}) -> {result.error or 'ok'}")
        if result.ok:
            applied.append(f"exposure_comp={_read_back(camera.get_exposure_compensation())}")
        else:
            failed.append(f"exposure_comp ({result.error})")

    for line in applied:
        print(f"read-back: {line}")
    print(f"trace: {out / 'trace.jsonl'}")
    if failed:
        print(f"\nFAILED to apply: {', '.join(failed)}")
        return 3
    return 0


def _read_back(result: object) -> object:
    """First element of a single-value getter response, or the error."""
    status = getattr(result, "ok", False)
    if not status:
        return getattr(result, "error", None)
    value = getattr(result, "result", None)
    if isinstance(value, list) and value:
        return value[0]
    return value


# -------------------------------------------------------------------- rec-mode

def cmd_rec_mode(args: argparse.Namespace) -> int:
    print(BANNER)
    out = artifact_dir("rec-mode")
    recorder = Recorder(out / "trace.jsonl")

    camera = _camera_for(args, recorder)
    if camera is None:
        return 2

    camera.load_capabilities()
    stream = EventStream(camera, recorder=recorder)

    rule("Pre-check: startRecMode capability and status")
    snapshot_before = stream.snapshot()
    status_before = (snapshot_before.events.get("cameraStatus") or {}).get("cameraStatus")
    print(f"camera status before : {status_before}")

    is_supported = camera.capabilities.supports("startRecMode")
    is_available = camera.capabilities.is_available("startRecMode")
    print(f"startRecMode supported: {is_supported}")
    print(f"startRecMode available: {is_available}")

    if not is_supported or not is_available:
        if not is_supported:
            print("\nstartRecMode is not supported by camera firmware.")
        else:
            print("\nstartRecMode is currently not available in this camera state.")
        artifact_data = {
            "command": "rec-mode",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "endpoint": camera.endpoint,
            "simulated": camera.is_simulated,
            "camera_status_before": status_before,
            "start_rec_mode_supported": is_supported,
            "start_rec_mode_available": is_available,
            "rpc_result": None,
            "camera_status_after": None,
            "available_apis_after": None,
            "event_snapshot_before": snapshot_before.events,
            "event_snapshot_after": None,
            "aborted_reason": "not supported" if not is_supported else "not available",
        }
        (out / "rec-mode.json").write_text(json.dumps(artifact_data, indent=2), encoding="utf-8")
        print(f"\nartifact: {out / 'rec-mode.json'}")
        print(f"trace   : {out / 'trace.jsonl'}")
        return 3

    rule("Execute startRecMode (single attempt, no retry)")
    res = camera.start_rec_mode()
    print(f"startRecMode -> {'ok' if res.ok else 'error'} (latency {res.latency_ms:.1f} ms)")
    if res.error:
        print(f"error code: {res.error[0]}, message: {res.error[1]}")
    elif res.transport_error:
        print(f"transport error: {res.transport_error}")

    snapshot_after = None
    status_after = None
    available_api_list = None
    if res.ok:
        rule("Post-check: event snapshot (polling=false)")
        snapshot_after = stream.snapshot()
        status_after = (snapshot_after.events.get("cameraStatus") or {}).get("cameraStatus")
        available_api_list = (snapshot_after.events.get("availableApiList") or {}).get("names", [])
        print(f"camera status after  : {status_after}")
        print(f"available APIs ({len(available_api_list)}): {available_api_list}")

    artifact_data = {
        "command": "rec-mode",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": camera.endpoint,
        "simulated": camera.is_simulated,
        "camera_status_before": status_before,
        "start_rec_mode_supported": is_supported,
        "start_rec_mode_available": is_available,
        "rpc_result": {
            "ok": res.ok,
            "http_status": res.http_status,
            "latency_ms": round(res.latency_ms, 1),
            "result": res.result,
            "error": res.error,
            "transport_error": res.transport_error,
        },
        "camera_status_after": status_after,
        "available_apis_after": available_api_list,
        "event_snapshot_before": snapshot_before.events,
        "event_snapshot_after": snapshot_after.events if snapshot_after else None,
    }
    artifact_path = out / "rec-mode.json"
    artifact_path.write_text(json.dumps(artifact_data, indent=2), encoding="utf-8")

    print(f"\nartifact: {artifact_path}")
    print(f"trace   : {out / 'trace.jsonl'}")
    return 0 if res.ok else 4


# ------------------------------------------------------------------------ sim

def cmd_sim(args: argparse.Namespace) -> int:
    from reticle0 import sim

    return sim.main(["--port", str(args.port)])


# ------------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description=BANNER)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--endpoint", help="skip discovery, e.g. http://192.168.122.1:10000/sony/camera")
    common.add_argument("--interface", help="force a local source address for discovery")
    common.add_argument("--mx", type=int, default=2, help="SSDP MX seconds (default 2)")

    sub.add_parser("netinfo", help="Phase A network baseline")

    discover = sub.add_parser("discover", parents=[common], help="Phase B SSDP discovery")
    discover.add_argument("--scan", action="store_true", help="fall back to a TCP sweep")
    discover.set_defaults(func=cmd_discover)

    probe = sub.add_parser("probe", parents=[common], help="Phase C capability probe")
    probe.add_argument("--publish", action="store_true", default=True,
                       help="write docs/phase0/02-capability-matrix.md (default on)")
    probe.add_argument("--no-publish", dest="publish", action="store_false")
    probe.set_defaults(func=cmd_probe)

    events = sub.add_parser("events", parents=[common], help="Phase E event stream")
    events.add_argument("--count", type=int, default=5)
    events.add_argument("--seconds", type=float, default=60.0)
    events.set_defaults(func=cmd_events)

    capture = sub.add_parser("capture", parents=[common], help="Phase D single capture")
    capture.add_argument("--shutter", help="set this shutter speed before capturing: 1/125, 20 (seconds), or an exact value from probe")
    capture.add_argument("--budget", type=float, default=None, help="capture timeout budget in seconds")
    capture.set_defaults(func=cmd_capture)

    setting = sub.add_parser("set", parents=[common], help="apply one exposure setting")
    setting.add_argument("--shutter", help="1/125, 20 (seconds), or an exact value from probe")
    setting.add_argument("--iso")
    setting.add_argument("--fnumber")
    setting.add_argument("--exposure-comp", type=int, help="exposure compensation step index (e.g. 0, 1, -2)")
    setting.add_argument("--dry-run", action="store_true")
    setting.set_defaults(func=cmd_set)

    rec_mode = sub.add_parser("rec-mode", parents=[common], help="explicitly issue startRecMode")
    rec_mode.set_defaults(func=cmd_rec_mode)

    sim = sub.add_parser("sim", help="run the offline simulated camera")
    sim.add_argument("--port", type=int, default=10000)
    sim.set_defaults(func=cmd_sim)

    sub.choices["netinfo"].set_defaults(func=cmd_netinfo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
