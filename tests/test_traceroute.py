"""Tests for network/traceroute.py - OS traceroute topology discovery."""
from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from network.traceroute import (
    DiscoveredPath,
    Hop,
    TracerouteResult,
    _get_platform,
    _is_windows_output,
    _parse_traceroute_output,
    _parse_tracert_output,
    discover_topology_from_traceroute,
    run_traceroute,
)
from tools.live_collector import DEFAULT_TRACEROUTE_DESTINATIONS


class TestPlatformDetection:
    """Test platform detection logic."""

    def test_get_platform_windows(self):
        """Test Windows platform detection."""
        with mock.patch("sys.platform", "win32"):
            assert _get_platform() == "win32"

    def test_get_platform_linux(self):
        """Test Linux platform detection."""
        with mock.patch("sys.platform", "linux"):
            assert _get_platform() == "linux"

    def test_get_platform_darwin(self):
        """Test macOS platform detection."""
        with mock.patch("sys.platform", "darwin"):
            assert _get_platform() == "darwin"

    def test_get_platform_unknown(self):
        """Test unknown platform."""
        with mock.patch("sys.platform", "unknown_platform"):
            assert _get_platform() == "unknown"


class TestWindowsOutputDetection:
    """Test Windows tracert output detection."""

    def test_is_windows_output_true(self):
        """Test Windows output detection."""
        windows_output = "Microsoft Windows [Version 10.0.19041.1]\nTracing route to 1.1.1.1"
        assert _is_windows_output(windows_output) is True

    def test_is_windows_output_case_insensitive(self):
        """Test case-insensitive Windows output detection."""
        windows_output = "microsoft windows [version 10.0]\nTracing route to 1.1.1.1"
        assert _is_windows_output(windows_output) is True

    def test_is_windows_output_false(self):
        """Test non-Windows output detection."""
        linux_output = "traceroute to 1.1.1.1 (1.1.1.1), 30 hops max"
        assert _is_windows_output(linux_output) is False


class TestTracertOutputParsing:
    """Test Windows tracert output parsing."""

    def test_parse_tracert_output_basic(self):
        """Test parsing basic Windows tracert output."""
        output = """Tracing route to 1.1.1.1 over a maximum of 30 hops

  1     1 ms     1 ms     1 ms  192.168.1.1
  2     2 ms     1 ms     2 ms  10.0.0.1
  3    *        *        *     Request timed out.
  4     5 ms     6 ms     4 ms  203.0.113.45
Trace complete."""
        hops = _parse_tracert_output("1.1.1.1", output)
        
        assert len(hops) == 4
        
        # First hop
        assert hops[0].ttl == 1
        assert hops[0].address == "192.168.1.1"
        assert hops[0].rtt1 == 1.0
        assert hops[0].rtt2 == 1.0
        assert hops[0].rtt3 == 1.0
        assert hops[0].responsive is True
        
        # Second hop
        assert hops[1].ttl == 2
        assert hops[1].address == "10.0.0.1"
        assert hops[1].rtt1 == 2.0
        assert hops[1].rtt2 == 1.0
        assert hops[1].rtt3 == 2.0
        assert hops[1].responsive is True
        
        # Third hop (timeout)
        assert hops[2].ttl == 3
        assert hops[2].address is None
        assert hops[2].responsive is False
        
        # Fourth hop
        assert hops[3].ttl == 4
        assert hops[3].address == "203.0.113.45"
        assert hops[3].rtt1 == 5.0
        assert hops[3].rtt2 == 6.0
        assert hops[3].rtt3 == 4.0
        assert hops[3].responsive is True

    def test_parse_tracert_output_with_hostnames(self):
        """Test parsing with hostnames and IPs."""
        output = """Tracing route to example.com [93.184.216.34]

  1     1 ms     1 ms     1 ms  router.example.com [192.168.1.1]
  2     2 ms     3 ms     2 ms  10.0.0.1
Trace complete."""
        hops = _parse_tracert_output("example.com", output)
        
        assert len(hops) == 2
        assert hops[0].hostname == "router.example.com"
        assert hops[0].address == "192.168.1.1"
        assert hops[1].address == "10.0.0.1"

    def test_parse_tracert_output_empty(self):
        """Test parsing empty output."""
        hops = _parse_tracert_output("1.1.1.1", "")
        assert hops == []


class TestTracerouteOutputParsing:
    """Test Unix traceroute output parsing."""

    def test_parse_traceroute_output_basic(self):
        """Test parsing basic Unix traceroute output."""
        output = """traceroute to 1.1.1.1 (1.1.1.1), 30 hops max, 60 byte packets
 1  gateway (192.168.1.1)  1.234 ms  1.567 ms  1.890 ms
 2  10.0.0.1 (10.0.0.1)  2.345 ms  3.456 ms  4.567 ms
 3  * * *
 4  203.0.113.45 (203.0.113.45)  5.678 ms  6.789 ms  7.890 ms"""
        hops = _parse_traceroute_output("1.1.1.1", output)
        
        assert len(hops) == 4
        
        # First hop
        assert hops[0].ttl == 1
        assert hops[0].hostname == "gateway"
        assert hops[0].address == "192.168.1.1"
        assert hops[0].rtt1 == pytest.approx(1.234)
        assert hops[0].rtt2 == pytest.approx(1.567)
        assert hops[0].rtt3 == pytest.approx(1.890)
        
        # Fourth hop
        assert hops[3].ttl == 4
        assert hops[3].address == "203.0.113.45"

    def test_parse_traceroute_output_empty(self):
        """Test parsing empty output."""
        hops = _parse_traceroute_output("1.1.1.1", "")
        assert hops == []


class TestRunTraceroute:
    """Test run_traceroute function."""

    def test_run_traceroute_unsupported_platform(self):
        """Test traceroute on unsupported platform."""
        with mock.patch("sys.platform", "unknown_platform"):
            result = run_traceroute("1.1.1.1", timeout=1)
            assert result.success is False
            assert result.error is not None
            assert "not supported" in result.error.lower()

    @mock.patch("subprocess.Popen")
    def test_run_traceroute_windows(self, mock_popen):
        """Test Windows tracert execution."""
        # Mock process
        mock_process = mock.MagicMock()
        # With text=True, communicate returns strings
        mock_process.communicate.return_value = (
            "Tracing route to 1.1.1.1\r\n  1     1 ms     1 ms     1 ms  192.168.1.1\r\n",
            ""
        )
        mock_process.returncode = 0
        mock_popen.return_value = mock_process
        
        with mock.patch("sys.platform", "win32"):
            result = run_traceroute("1.1.1.1", timeout=10)
            
            assert result.success is True
            assert len(result.hops) >= 1
            assert result.hops[0].ttl == 1
            
            # Verify argv was correct (no shell - it's the default)
            call_args = mock_popen.call_args
            assert "1.1.1.1" in call_args[0][0]

    @mock.patch("subprocess.Popen")
    def test_run_traceroute_linux(self, mock_popen):
        """Test Linux traceroute execution."""
        mock_process = mock.MagicMock()
        # With text=True, communicate returns strings
        mock_process.communicate.return_value = (
            "traceroute to 1.1.1.1 (1.1.1.1), 30 hops max\n 1  192.168.1.1  1.0 ms  1.0 ms  1.0 ms\n",
            ""
        )
        mock_process.returncode = 0
        mock_popen.return_value = mock_process
        
        with mock.patch("sys.platform", "linux"):
            result = run_traceroute("1.1.1.1", timeout=10)
            
            assert result.success is True
            assert len(result.hops) >= 1
            
            # Verify argv was correct (no shell - it's the default)
            call_args = mock_popen.call_args
            assert "1.1.1.1" in call_args[0][0]

    @mock.patch("subprocess.Popen")
    def test_run_traceroute_timeout(self, mock_popen):
        """Test traceroute timeout handling."""
        mock_process = mock.MagicMock()
        # First call raises TimeoutExpired, second call returns empty strings
        mock_process.communicate.side_effect = [
            subprocess.TimeoutExpired("tracert", 10),
            ("", "")
        ]
        mock_process.returncode = -1
        mock_popen.return_value = mock_process
        
        with mock.patch("sys.platform", "win32"):
            result = run_traceroute("1.1.1.1", timeout=1)
            
            # Should still complete (with error)
            assert result.success is False
            assert result.error is not None

    @mock.patch("subprocess.Popen")
    def test_run_traceroute_failure(self, mock_popen):
        """Test traceroute command failure."""
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (b"", b"Error: destination host unreachable")
        mock_process.returncode = 1
        mock_popen.return_value = mock_process
        
        with mock.patch("sys.platform", "win32"):
            result = run_traceroute("192.0.2.1", timeout=1)
            
            assert result.success is False
            assert result.error is not None


class TestDiscoverTopology:
    """Test topology discovery from traceroute."""

    @mock.patch("network.traceroute.run_traceroute")
    def test_discover_topology_single_path(self, mock_run):
        """Test topology discovery with a single path."""
        # Mock traceroute result
        hops = [
            Hop(ttl=1, address="192.168.1.1", hostname="router1", 
                 rtt1=1.0, rtt2=1.0, rtt3=1.0, resolved=True, responsive=True),
            Hop(ttl=2, address="10.0.0.1", hostname="router2",
                 rtt1=2.0, rtt2=2.0, rtt3=2.0, resolved=True, responsive=True),
        ]
        mock_run.return_value = TracerouteResult(
            destination="1.1.1.1", success=True, hops=hops)
        
        result = discover_topology_from_traceroute(["1.1.1.1"])
        
        assert result is not None
        assert len(result.devices) == 2
        assert len(result.links) >= 1
        assert len(result.metrics) >= 6  # 3 RTT values per hop * 2 hops
        
        # Check device names
        device_names = {d["name"] for d in result.devices}
        assert "192.168.1.1" in device_names
        assert "10.0.0.1" in device_names

    @mock.patch("network.traceroute.run_traceroute")
    def test_discover_topology_multiple_paths(self, mock_run):
        """Test topology discovery with multiple paths (shared hops)."""
        # First path: A -> B -> C
        hops1 = [
            Hop(ttl=1, address="192.168.1.1", hostname=None, rtt1=1.0, 
                 rtt2=None, rtt3=None, resolved=True, responsive=True),
            Hop(ttl=2, address="10.0.0.1", hostname=None,
                 rtt1=2.0, rtt2=None, rtt3=None, resolved=True, responsive=True),
            Hop(ttl=3, address="203.0.113.1", hostname=None,
                 rtt1=3.0, rtt2=None, rtt3=None, resolved=True, responsive=True),
        ]
        # Second path: A -> B -> D (shares first two hops)
        hops2 = [
            Hop(ttl=1, address="192.168.1.1", hostname=None,
                 rtt1=1.5, rtt2=None, rtt3=None, resolved=True, responsive=True),
            Hop(ttl=2, address="10.0.0.1", hostname=None,
                 rtt1=2.5, rtt2=None, rtt3=None, resolved=True, responsive=True),
            Hop(ttl=3, address="203.0.113.2", hostname=None,
                 rtt1=3.5, rtt2=None, rtt3=None, resolved=True, responsive=True),
        ]
        
        mock_run.side_effect = [
            TracerouteResult(destination="1.1.1.1", success=True, hops=hops1),
            TracerouteResult(destination="8.8.8.8", success=True, hops=hops2),
        ]
        
        result = discover_topology_from_traceroute(["1.1.1.1", "8.8.8.8"])
        
        assert result is not None
        # Should have 4 unique devices (192.168.1.1, 10.0.0.1, 203.0.113.1, 203.0.113.2)
        assert len(result.devices) == 4
        # Should have links between them
        assert len(result.links) >= 3

    @mock.patch("network.traceroute.run_traceroute")
    def test_discover_topology_with_timeouts(self, mock_run):
        """Test topology discovery with timeout hops."""
        hops = [
            Hop(ttl=1, address="192.168.1.1", hostname=None,
                 rtt1=1.0, rtt2=None, rtt3=None, resolved=True, responsive=True),
            Hop(ttl=2, address=None, hostname=None,
                 rtt1=None, rtt2=None, rtt3=None, resolved=False, responsive=False),
            Hop(ttl=3, address="10.0.0.1", hostname=None,
                 rtt1=3.0, rtt2=None, rtt3=None, resolved=True, responsive=True),
        ]
        mock_run.return_value = TracerouteResult(
            destination="1.1.1.1", success=True, hops=hops)
        
        result = discover_topology_from_traceroute(["1.1.1.1"])
        
        assert result is not None
        # Should only create devices for hops with addresses
        assert len(result.devices) == 2
        assert "192.168.1.1" in {d["name"] for d in result.devices}
        assert "10.0.0.1" in {d["name"] for d in result.devices}

    @mock.patch("network.traceroute.run_traceroute")
    def test_discover_topology_failed_traceroute(self, mock_run):
        """Test topology discovery when traceroute fails."""
        mock_run.return_value = TracerouteResult(
            destination="1.1.1.1", success=False, error="Command not found")
        
        result = discover_topology_from_traceroute(["1.1.1.1"])
        
        # Should return empty DiscoveredPath
        assert result is not None
        assert len(result.devices) == 0


class TestDefaultDestinations:
    """Test default traceroute destinations."""

    def test_default_destinations_exist(self):
        """Test that default destinations are defined."""
        assert DEFAULT_TRACEROUTE_DESTINATIONS is not None
        assert len(DEFAULT_TRACEROUTE_DESTINATIONS) > 0
        
        # Check they're all valid IPs or hostnames
        for dest in DEFAULT_TRACEROUTE_DESTINATIONS:
            assert isinstance(dest, str)
            assert len(dest) > 0


class TestMalformedOutput:
    """Test handling of malformed traceroute output."""

    def test_parse_malformed_tracert(self):
        """Test parsing malformed Windows output."""
        malformed_output = "This is not a valid tracert output\nwith random text\n123"
        hops = _parse_tracert_output("1.1.1.1", malformed_output)
        assert hops == []

    def test_parse_malformed_traceroute(self):
        """Test parsing malformed Unix output."""
        malformed_output = "Random text\nwith numbers 1 2 3\nbut no structure"
        hops = _parse_traceroute_output("1.1.1.1", malformed_output)
        assert hops == []

    def test_parse_injection_attempt(self):
        """Test that injection attempts are handled safely."""
        # This shouldn't cause any issues - the parser just won't find valid patterns
        malicious_output = "1 ; rm -rf /; 1 ms 1 ms 1 ms 192.168.1.1"
        hops = _parse_tracert_output("1.1.1.1", malicious_output)
        # Should either parse the valid part or return empty
        assert isinstance(hops, list)


class TestDataClasses:
    """Test dataclass instantiation."""

    def test_hop_dataclass(self):
        """Test Hop dataclass."""
        hop = Hop(
            ttl=1,
            address="192.168.1.1",
            hostname="router",
            rtt1=1.0,
            rtt2=2.0,
            rtt3=3.0,
            resolved=True,
            responsive=True
        )
        assert hop.ttl == 1
        assert hop.address == "192.168.1.1"
        assert hop.responsive is True

    def test_traceroute_result_dataclass(self):
        """Test TracerouteResult dataclass."""
        result = TracerouteResult(
            destination="1.1.1.1",
            success=True,
            hops=[],
            error=None
        )
        assert result.destination == "1.1.1.1"
        assert result.success is True

    def test_discovered_path_dataclass(self):
        """Test DiscoveredPath dataclass."""
        path = DiscoveredPath(
            destination="1.1.1.1",
            devices=[{"name": "192.168.1.1", "type": "router"}],
            links=[{"device_a": "192.168.1.1", "device_b": "10.0.0.1"}],
            metrics=[{"device_name": "192.168.1.1", "metric": "rtt", "value": 1.0}]
        )
        assert path.destination == "1.1.1.1"
        assert len(path.devices) == 1
        assert len(path.links) == 1
        assert len(path.metrics) == 1
