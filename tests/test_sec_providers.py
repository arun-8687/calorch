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


# ---------------------------------------------------------------------------
# latest_fundamentals: quarterly-vs-annual period selection
# ---------------------------------------------------------------------------
def _companyfacts_with_revenue_entries(entries: list[dict]) -> dict:
    return {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": entries}},
            },
            "dei": {},
        }
    }


def test_latest_fundamentals_prefers_quarterly_over_later_annual_end(monkeypatch: pytest.MonkeyPatch):
    """A 10-K's full-fiscal-year period can have a LATER end date than the
    most recent 10-Q quarter. Sorting purely by "end" would report the
    annual figure as "the quarter" — assert the quarterly entry wins even
    though its end date is earlier.
    """
    entries = [
        # Full fiscal year: 2024-09-30 -> 2025-09-27 (~362 days), later end.
        {"start": "2024-09-30", "end": "2025-09-27", "val": 391_035_000_000,
         "form": "10-K", "frame": None},
        # Most recent quarter: 2025-06-29 -> 2025-09-27 would tie the FY end;
        # use a genuinely earlier end so the "later end" case is exercised.
        {"start": "2025-03-30", "end": "2025-06-28", "val": 94_036_000_000,
         "form": "10-Q", "frame": "CY2025Q2"},
    ]
    cf = _companyfacts_with_revenue_entries(entries)
    client = SecIxbrlClient(user_agent="test", cache_dir=Path("/tmp/nope"))
    monkeypatch.setattr(client, "_fetch_companyfacts", lambda cik: cf)

    result = client.latest_fundamentals("0000320193", "AAPL")
    assert result["revenue"] == 94_036_000_000
    assert result["revenue_period"] == "2025-06-28"
    assert result["revenue_form"] == "10-Q"


def test_latest_fundamentals_prefers_quarterly_when_same_latest_end(monkeypatch: pytest.MonkeyPatch):
    """Same latest end date, one annual (~365d) one quarterly (~90d) entry —
    the quarterly entry must still win."""
    entries = [
        {"start": "2024-09-29", "end": "2025-09-27", "val": 391_035_000_000,
         "form": "10-K", "frame": None},
        {"start": "2025-06-29", "end": "2025-09-27", "val": 102_466_000_000,
         "form": "10-Q", "frame": "CY2025Q3"},
    ]
    cf = _companyfacts_with_revenue_entries(entries)
    client = SecIxbrlClient(user_agent="test", cache_dir=Path("/tmp/nope"))
    monkeypatch.setattr(client, "_fetch_companyfacts", lambda cik: cf)

    result = client.latest_fundamentals("0000320193", "AAPL")
    assert result["revenue"] == 102_466_000_000
    assert result["revenue_period"] == "2025-09-27"
    assert result["revenue_form"] == "10-Q"


def test_latest_fundamentals_instant_concept_unaffected(monkeypatch: pytest.MonkeyPatch):
    """Instant concepts (no "start") keep plain latest-end ordering."""
    cf = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {"end": "2025-06-28", "val": 300_000_000_000, "form": "10-Q", "frame": None},
                            {"end": "2025-09-27", "val": 364_980_000_000, "form": "10-K", "frame": "CY2025Q3I"},
                        ]
                    }
                },
            },
            "dei": {},
        }
    }
    client = SecIxbrlClient(user_agent="test", cache_dir=Path("/tmp/nope"))
    monkeypatch.setattr(client, "_fetch_companyfacts", lambda cik: cf)

    result = client.latest_fundamentals("0000320193", "AAPL")
    assert result["total_assets"] == 364_980_000_000
    assert result["total_assets_period"] == "2025-09-27"
    assert result["total_assets_form"] == "10-K"


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


# ---------------------------------------------------------------------------
# humanize_member — XBRL member names rendered as analyst-readable labels
# ---------------------------------------------------------------------------
import pytest as _pytest  # noqa: E402

from calorch.sec_ixbrl import humanize_member  # noqa: E402


@_pytest.mark.parametrize(
    ("member", "expected"),
    [
        # Leading-lowercase brands survive CamelCase splitting.
        ("aapl:IPhoneMember", "iPhone"),
        ("IPadMember", "iPad"),
        # Plain members, namespace stripped, suffix removed.
        ("MacMember", "Mac"),
        ("ServiceMember", "Service"),
        ("msft:IntelligentCloudMember", "Intelligent Cloud"),
        ("nvda:DataCenterMember", "Data Center"),
        # Structural suffixes stack ("...SegmentMember").
        ("AmericasSegmentMember", "Americas"),
        ("GreaterChinaSegmentMember", "Greater China"),
        # Connectives XBRL embeds in lowercase, and small words kept lowercase.
        ("RestOfAsiaPacificSegmentMember", "Rest of Asia Pacific"),
        ("WearablesHomeandAccessoriesMember", "Wearables Home and Accessories"),
        ("OfficeProductsAndCloudServicesMember", "Office Products and Cloud Services"),
    ],
)
def test_humanize_member(member: str, expected: str) -> None:
    assert humanize_member(member) == expected


def test_humanize_member_degrades_to_input() -> None:
    """Nothing left to show -> return something traceable, never blank."""
    assert humanize_member("Member") == "Member"
    assert humanize_member("") == ""
    assert humanize_member("aapl:Member") == "Member"
