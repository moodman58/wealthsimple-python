import requests
from typing import Optional


def quote(ticker: str, source: str = 'wealthsimple', asset_class: str = 'stocks') -> Optional[float]:
    """
    Get a quick quote for a ticker symbol.

    Args:
        ticker: Stock ticker symbol
        source: Quote source ('wealthsimple', 'yahoo', 'nasdaq', 'tsx')
        asset_class: Asset class for nasdaq source (default: 'stocks')

    Returns:
        Current price or None if unavailable
    """
    if source.lower() == 'wealthsimple':
        from .client import WealthsimpleV2
        ws = WealthsimpleV2()
        security_id = ws.get_ticker_id(ticker)
        if security_id:
            quote_data = ws.get_security_quote(security_id)
            return quote_data.get('price')
        return None

    elif source.lower() == 'yahoo':
        try:
            r = requests.get(
                f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}',
                timeout=3
            )
            return float(r.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception:
            return None

    elif source.lower() == 'nasdaq':
        try:
            r = requests.get(
                f'https://api.nasdaq.com/api/quote/{ticker}/info',
                params={'assetclass': asset_class},
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': '*/*'},
                timeout=3
            )
            price_str = r.json()['data']['primaryData']['lastSalePrice'].strip('$')
            return float(price_str)
        except Exception:
            return None

    elif source.lower() in ('tsx', 'tmx'):
        try:
            r = requests.post(
                'https://app-money.tmx.com/graphql',
                json={
                    "operationName": "getQuoteBySymbol",
                    "variables": {"symbol": ticker, "locale": "en"},
                    "query": "query getQuoteBySymbol($symbol: String, $locale: String) { getQuoteBySymbol(symbol: $symbol, locale: $locale) { symbol name price }}"
                },
                timeout=3
            )
            return float(r.json()['data']['getQuoteBySymbol']['price'])
        except Exception:
            return None

    return None
