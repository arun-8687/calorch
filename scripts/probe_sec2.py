import json
from calorch.config import get_settings
from calorch.sec import SecEdgarClient
from datetime import datetime, timedelta, UTC

s = get_settings()
print('user_agent:', s.sec_user_agent)
print('watchlist:', s.sec_watchlist)
sec = SecEdgarClient(user_agent=s.sec_user_agent, cache_dir=s.sec_cache_dir)
end = datetime.now(UTC)
start = end - timedelta(days=7)
print(f'window: {start.date()} -> {end.date()}')
events = sec.list_recent_filings(s.sec_watchlist, start.date(), end.date())
print(f'filings found: {len(events)}')
for e in events[:5]:
    tk = e.get('_ticker', '?')
    form = e.get('_form', '?')
    fd = e.get('_filingDate', '?')
    subj = e.get('subject', '')[:60]
    print(f'  {tk:<6} {form:<10} {fd}  {subj}')

print()
print('--- AAPL latest XBRL facts ---')
print(json.dumps(sec.latest_financials('AAPL'), indent=2, default=str))
