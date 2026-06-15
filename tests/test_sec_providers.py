"""Tests for the SEC iXBRL segment parser + SEC EFTS full-text search."""
from __future__ import annotations

from pathlib import Path

import pytest

from calorch.sec_ixbrl import SecIxbrlClient, _strip_ns, _to_float
from calorch.sec import SecEdgarClient


# ---------------------------------------------------------------------------
# iXBRL parser unit tests
# ---------------------------------------------------------------------------
SAMPLE_IXBRL = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:us-gaap="http://fasb.org/us-gaap/2024"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:aapl="http://www.apple.com/20240928">
  <context id="FY26Q1_iPhone">
    <entity>
      <identifier scheme="http://www.sec.gov/CIK">0000320193</identifier>
      <segment>
        <explicitMember dimension="us-gaap:ProductOrServiceAxis">aapl:IPhoneMember</explicitMember>
      </segment>
    </entity>
    <period>
      <startDate>2025-09-29</startDate>
      <endDate>2025-12-27</endDate>
    </period>
  </context>
  <context id="FY26Q1_Mac">
    <entity>
      <identifier scheme="http://www.sec.gov/CIK">0000320193</identifier>
      <segment>
        <explicitMember dimension="us-gaap:ProductOrServiceAxis">aapl:MacMember</explicitMember>
      </segment>
    </entity>
    <period>
      <startDate>2025-09-29</startDate>
      <endDate>2025-12-27</endDate>
    </period>
  </context>
  <context id="FY26Q1_Total">
    <entity>
      <identifier scheme="http://www.sec.gov/CIK">0000320193</identifier>
    </entity>
    <period>
      <startDate>2025-09-29</startDate>
      <endDate>2025-12-27</endDate>
    </period>
  </context>
  <!-- Real iXBRL uses values in millions (decimals="-6") -->
  <us-gaap:Revenues contextRef="FY26Q1_iPhone" unitRef="USD" decimals="-6">69138</us-gaap:Revenues>
  <us-gaap:Revenues contextRef="FY26Q1_Mac" unitRef="USD" decimals="-6">8388</us-gaap:Revenues>
  <us-gaap:Revenues contextRef="FY26Q1_Total" unitRef="USD" decimals="-6">124300</us-gaap:Revenues>
</xbrl>
"""


def test_strip_ns_removes_namespace_prefix():
    assert _strip_ns("us-gaap:Revenues") == "Revenues"
    assert _strip_ns("aapl:IPhoneMember") == "IPhoneMember"
    assert _strip_ns("no_prefix") == "no_prefix"


def test_to_float_handles_fred_missing_dot():
    assert _to_float("123.45") == 123.45
    assert _to_float(".") is None
    assert _to_float("") is None
    assert _to_float(None) is None


def test_extract_segment_facts_pulls_only_segment_tagged():
    parser = SecIxbrlClient(user_agent="test", cache_dir=Path("/tmp/nope"))
    facts = parser.extract_segment_facts(SAMPLE_IXBRL)
    # iPhone + Mac, not the consolidated total
    assert len(facts) == 2
    members = {f.segment_member for f in facts}
    assert members == {"IPhoneMember", "MacMember"}
    # values preserved
    iphone = next(f for f in facts if f.segment_member == "IPhoneMember")
    assert iphone.value == 69_138_000_000.0
    assert iphone.axis == "ProductOrServiceAxis"
    assert iphone.period_end == "2025-12-27"


def test_extract_segment_facts_handles_garbage_input():
    parser = SecIxbrlClient(user_agent="test", cache_dir=Path("/tmp/nope"))
    assert parser.extract_segment_facts(b"<not xml") == []
    assert parser.extract_segment_facts(b"") == []


def test_sec_edgar_client_get_uses_shared_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify SecEdgarClient._get() delegates to the shared HTTP client."""
    from unittest.mock import MagicMock

    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}
    mock_response.status_code = 200

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    monkeypatch.setattr("calorch.sec.get_client", lambda: mock_client)

    client = SecEdgarClient("test-agent", cache_dir=tmp_path)
    client._rl.wait = lambda: None
    result = client._get("https://data.sec.gov/test")
    assert result == {"ok": True}
    assert mock_client.get.called
