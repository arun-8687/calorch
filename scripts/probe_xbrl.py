import json, httpx
ua = 'Calorch Research calorch@example.com'
data = httpx.get('https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json', headers={'User-Agent': ua}, timeout=30).json()
us_gaap = data['facts']['us-gaap']
entries = us_gaap.get('Revenues', {}).get('units', {}).get('USD', [])
print('Total Revenue entries:', len(entries))
print('Top 10 by (end, filed) desc:')
sorted_e = sorted(entries, key=lambda e: (e.get('end',''), e.get('filed','')), reverse=True)
for e in sorted_e[:10]:
    end = e.get('end')
    filed = e.get('filed')
    form = e.get('form')
    fp = e.get('fp')
    val = e.get('val')
    fy = e.get('fy')
    print(f'  end={end}  filed={filed}  form={form:<6} fp={fp}  fy={fy}  val={val:>15,}')
