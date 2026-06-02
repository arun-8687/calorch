import json
d = json.load(open(r'C:\Users\arun8\AppData\Local\Temp\company_tickers.json'))
print('count:', len(d))
keep = ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'TSLA', 'BRK.B', 'JPM', 'V', 'WMT', 'XOM', 'JNJ', 'PG', 'MA', 'HD', 'ABBV', 'AVGO', 'BAC']
for k, v in d.items():
    if v['ticker'] in keep:
        print(f'  {v["ticker"]:<6} CIK={v["cik_str"]:>10}  {v["title"][:50]}')
