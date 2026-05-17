import requests
import json
import os
import time
import base64
import uuid
from typing import Dict, List, Optional
from datetime import datetime, timedelta

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    keyring = None

try:
    from .subscriptions import WealthsimpleSubscriptions, WEBSOCKETS_AVAILABLE
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WealthsimpleSubscriptions = None


class WealthsimpleV2:
    """
    Unofficial Wealthsimple API v2 Client using the GraphQL endpoint.

    Tokens are stored securely via keyring (OS credential store) when available,
    falling back to environment variables WS_ACCESS_TOKEN / WS_REFRESH_TOKEN.
    """

    KEYRING_SERVICE = "wealthsimple-python"

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None,
                 otp: Optional[str] = None, client_id: Optional[str] = None,
                 access_token: Optional[str] = None, refresh_token: Optional[str] = None):
        self.api_url = "https://my.wealthsimple.com/graphql"
        self.auth_url = "https://api.production.wealthsimple.com/v1/oauth/v2/token"
        self.client_id = client_id or "4da53ac2b03225bed1550eba8e4611e086c7b905a3855e6ed12ea08c246758fa"

        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expiry = None
        self.identity_id = None
        self.profiles = None

        if username and password:
            self.authenticate(username, password, otp)
        elif not access_token:
            if self._load_tokens_from_keyring():
                self._fetch_identity_id_from_token()
            else:
                env_access_token = os.getenv('WS_ACCESS_TOKEN')
                env_refresh_token = os.getenv('WS_REFRESH_TOKEN')

                if env_access_token and env_refresh_token:
                    self.access_token = env_access_token
                    self.refresh_token = env_refresh_token
                    self._fetch_identity_id_from_token()
                else:
                    username = os.getenv('WS_USERNAME')
                    password = os.getenv('WS_PASSWORD')
                    otp = os.getenv('WS_OTP')
                    if username and password:
                        self.authenticate(username, password, otp)

    # ==================== Token Storage ====================

    def _save_tokens_to_keyring(self, username: Optional[str] = None) -> bool:
        if not KEYRING_AVAILABLE:
            return False
        keyring_username = username or os.getenv('WS_USERNAME') or 'default'
        try:
            saved_any = False
            if self.access_token:
                keyring.set_password(self.KEYRING_SERVICE, f"{keyring_username}_access_token", self.access_token)
                saved_any = True
            if self.refresh_token:
                keyring.set_password(self.KEYRING_SERVICE, f"{keyring_username}_refresh_token", self.refresh_token)
                saved_any = True
            if self.token_expiry:
                keyring.set_password(self.KEYRING_SERVICE, f"{keyring_username}_token_expiry", str(self.token_expiry))
                saved_any = True
            return saved_any
        except Exception as e:
            print(f"Failed to save to keyring: {e}")
            return False

    def _load_tokens_from_keyring(self, username: Optional[str] = None) -> bool:
        if not KEYRING_AVAILABLE:
            return False
        keyring_username = username or os.getenv('WS_USERNAME') or 'default'
        try:
            access_token = keyring.get_password(self.KEYRING_SERVICE, f"{keyring_username}_access_token")
            refresh_token = keyring.get_password(self.KEYRING_SERVICE, f"{keyring_username}_refresh_token")
            token_expiry_str = keyring.get_password(self.KEYRING_SERVICE, f"{keyring_username}_token_expiry")
            if access_token and refresh_token:
                self.access_token = access_token
                self.refresh_token = refresh_token
                if token_expiry_str:
                    try:
                        self.token_expiry = float(token_expiry_str)
                    except ValueError:
                        self.token_expiry = None
                return True
        except Exception:
            pass
        return False

    def _delete_tokens_from_keyring(self, username: Optional[str] = None) -> None:
        if not KEYRING_AVAILABLE:
            return
        keyring_username = username or os.getenv('WS_USERNAME') or 'default'
        for suffix in ('_access_token', '_refresh_token', '_token_expiry'):
            try:
                keyring.delete_password(self.KEYRING_SERVICE, f"{keyring_username}{suffix}")
            except Exception:
                pass

    # ==================== Authentication ====================

    def authenticate(self, username: str, password: str, otp: Optional[str] = None) -> Dict:
        """Authenticate with Wealthsimple using OAuth v2."""
        payload = {
            "grant_type": "password",
            "username": username,
            "password": password,
            "skip_provision": True,
            "scope": "invest.read invest.write trade.read trade.write tax.read tax.write",
            "client_id": self.client_id
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
        }
        if otp:
            headers["x-wealthsimple-otp"] = otp

        response = requests.post(self.auth_url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            self.access_token = data.get('access_token')
            self.refresh_token = data.get('refresh_token')
            expires_in = data.get('expires_in', 1800)
            self.token_expiry = time.time() + expires_in
            self.identity_id = data.get('identity_canonical_id')
            self.profiles = data.get('profiles', {})

            if not self.identity_id and self.access_token:
                self._fetch_identity_id_from_token()

            self._save_tokens_to_keyring('default')
            print(f"Saved tokens to keyring")

            if self.access_token:
                os.environ['WS_ACCESS_TOKEN'] = self.access_token
            if self.refresh_token:
                os.environ['WS_REFRESH_TOKEN'] = self.refresh_token

            return data
        else:
            raise Exception(f"Authentication failed: {response.status_code} - {response.text}")

    def _fetch_identity_id_from_token(self):
        """Extract identity ID from the JWT access token payload."""
        try:
            if self.access_token:
                parts = self.access_token.split('.')
                if len(parts) >= 2:
                    payload = parts[1]
                    payload += '=' * (4 - len(payload) % 4)
                    try:
                        decoded = base64.urlsafe_b64decode(payload)
                        token_data = json.loads(decoded)
                        for key in ['identity_canonical_id', 'identity_id', 'sub', 'user_id']:
                            if key in token_data:
                                value = token_data[key]
                                if isinstance(value, str) and value.startswith('identity-'):
                                    self.identity_id = value
                                    return
                    except (ValueError, KeyError):
                        pass
        except Exception:
            pass

    def logout(self) -> None:
        """Logout and clear all stored tokens."""
        self._delete_tokens_from_keyring()
        os.environ.pop('WS_ACCESS_TOKEN', None)
        os.environ.pop('WS_REFRESH_TOKEN', None)
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = None
        self.identity_id = None
        self.profiles = None

    def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token."""
        if not self.refresh_token:
            return False
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id
        }
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        try:
            response = requests.post(self.auth_url, json=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get('access_token')
                self.refresh_token = data.get('refresh_token')
                self.token_expiry = time.time() + data.get('expires_in', 1800)
                self._save_tokens_to_keyring()
                if self.access_token:
                    os.environ['WS_ACCESS_TOKEN'] = self.access_token
                if self.refresh_token:
                    os.environ['WS_REFRESH_TOKEN'] = self.refresh_token
                return True
        except Exception:
            pass
        return False

    def _ensure_authenticated(self):
        if not self.access_token:
            raise Exception("Not authenticated. Please call authenticate() first.")
        if self.token_expiry and (time.time() + 300) > self.token_expiry:
            if not self.refresh_access_token():
                raise Exception("Token expired and refresh failed. Please re-authenticate.")

    def _get_headers(self) -> Dict[str, str]:
        self._ensure_authenticated()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
            "x-ws-api-version": "12",
            "x-platform-os": "web",
            "x-ws-locale": "en-CA",
            "x-ws-profile": "trade"
        }

    def graphql_query(self, operation_name: str, query: str, variables: Optional[Dict] = None) -> Dict:
        """Execute a GraphQL query or mutation."""
        payload = {
            "operationName": operation_name,
            "query": query,
            "variables": variables or {}
        }
        response = requests.post(self.api_url, json=payload, headers=self._get_headers())
        if response.status_code == 200:
            data = response.json()
            if 'errors' in data:
                raise Exception(f"GraphQL errors: {data['errors']}")
            return data
        else:
            raise Exception(f"Request failed: {response.status_code} - {response.text}")

    # ==================== Security Search & Info ====================

    def search_securities(self, query: str, security_group_ids: Optional[List[str]] = None) -> List[Dict]:
        """Search for securities by ticker symbol or name."""
        gql_query = """
        query FetchSecuritySearchResult($query: String!, $securityGroupIds: [String!]) {
          securitySearch(input: {query: $query, securityGroupIds: $securityGroupIds}) {
            results {
              id
              buyable
              sellable
              optionsEligible
              securityType
              allowedOrderSubtypes
              status
              stock {
                symbol
                name
                primaryExchange
              }
              features
              logoUrl
              quoteV2(currency: null) {
                securityId
                currency
                price
                ... on EquityQuote {
                  marketStatus
                  close
                  high
                  low
                  open
                  volume: vol
                }
              }
            }
          }
        }
        """
        variables = {"query": query, "securityGroupIds": security_group_ids}
        result = self.graphql_query("FetchSecuritySearchResult", gql_query, variables)
        return result.get('data', {}).get('securitySearch', {}).get('results', [])

    def get_nearest_market_open(self) -> Dict:
        """Get the nearest market open payload."""
        gql_query = """
        query FetchNearestMarketOpen {
          nearestMarketOpen {
            currentTradeDay {
              date
              overnight { start end __typename }
              preMarket { start end __typename }
              regular { start end __typename }
              postMarket { start end __typename }
              __typename
            }
            exchangeName
            isTodayEarlyClose
            mic
            nextTradeDay {
              date
              overnight { start end __typename }
              preMarket { start end __typename }
              regular { start end __typename }
              postMarket { start end __typename }
              __typename
            }
            previousTradeDay {
              date
              overnight { start end __typename }
              preMarket { start end __typename }
              regular { start end __typename }
              postMarket { start end __typename }
              __typename
            }
            __typename
          }
        }
        """
        result = self.graphql_query("FetchNearestMarketOpen", gql_query, {})
        return result.get('data', {}).get('nearestMarketOpen', {})

    def get_market_buffer(self, country: str = 'CA', is_option: bool = False) -> float:
        """Get the market buffer multiplier for a country/asset type."""
        gql_query = """
        query FetchMarketBuffer($country: String!, $isOption: Boolean) {
          marketBuffer(country: $country, isOption: $isOption) {
            marketBuffer
            __typename
          }
        }
        """
        variables = {"country": country, "isOption": is_option}
        result = self.graphql_query("FetchMarketBuffer", gql_query, variables)
        buffer_str = result.get('data', {}).get('marketBuffer', {}).get('marketBuffer')
        return float(buffer_str) if buffer_str else 1.05

    def get_security(self, security_id: str, currency: Optional[str] = None) -> Dict:
        """Get detailed information about a security."""
        gql_query = """
        query FetchSecurity($securityId: ID!, $currency: Currency) {
          security(id: $securityId) {
            id
            active
            activeDate
            allowedOrderSubtypes
            buyable
            currency
            depositEligible
            features
            inactiveDate
            isVolatile
            logoUrl
            securityType
            sellable
            settleable
            status
            wsTradeEligible
            wsTradeIneligibilityReason
            optionsEligible
            equityTradingSessionType
            stock {
              description
              dividendFrequency
              name
              primaryExchange
              primaryMic
              symbol
            }
            fundamentals(currency: $currency) {
              avgVolume
              beta
              marketCap
              peRatio
              eps
              yield
              high52Week
              low52Week
              description
            }
            quoteV2(currency: $currency) {
              securityId
              currency
              price
              ask
              bid
              ... on EquityQuote {
                marketStatus
                close
                high
                low
                open
                volume: vol
                askSize
                bidSize
                last
                lastSize
                mid
              }
            }
            optionDetails {
              expiryDate
              maturity
              multiplier
              optionType
              osiSymbol
              strikePrice
              underlyingSecurity {
                id
                stock {
                  name
                  symbol
                  primaryExchange
                }
              }
            }
          }
        }
        """
        variables = {"securityId": security_id, "currency": currency}
        result = self.graphql_query("FetchSecurity", gql_query, variables)
        return result.get('data', {}).get('security', {})

    def get_security_quote(self, security_id: str, currency: Optional[str] = None) -> Dict:
        """Get real-time quote for a security."""
        gql_query = """
        query FetchSecurityQuoteV2($id: ID!, $currency: Currency = null) {
          security(id: $id) {
            id
            quoteV2(currency: $currency) {
              securityId
              ask
              bid
              currency
              price
              sessionPrice
              quotedAsOf
              previousBaseline
              ... on EquityQuote {
                marketStatus
                askSize
                bidSize
                close
                high
                last
                lastSize
                low
                open
                mid
                volume: vol
                referenceClose
              }
              ... on OptionQuote {
                marketStatus
                askSize
                bidSize
                close
                high
                last
                lastSize
                low
                open
                mid
                volume: vol
                breakEven
                inTheMoney
                liquidityStatus
                openInterest
                underlyingSpot
              }
            }
          }
        }
        """
        variables = {"id": security_id, "currency": currency}
        result = self.graphql_query("FetchSecurityQuoteV2", gql_query, variables)
        return result.get('data', {}).get('security', {}).get('quoteV2', {})

    def get_security_status(self, security_id: str) -> Dict:
        """Get the trading status of a security."""
        gql_query = """
        query FetchSecurityStatus($id: ID!) {
          security(id: $id) {
            id
            status
            optionDetails {
              underlyingSecurity {
                id
                status
              }
            }
            __typename
          }
        }
        """
        result = self.graphql_query("FetchSecurityStatus", gql_query, {"id": security_id})
        return result.get('data', {}).get('security', {})

    def get_intraday_chart_quotes(
        self,
        security_id: str,
        period: str = 'ONE_DAY',
        trading_session: Optional[str] = 'REGULAR',
        currency: Optional[str] = None,
        date: Optional[str] = None,
    ) -> List[Dict]:
        """Get chart bar quotes for a security."""
        if trading_session is not None:
            gql_query = """
            query FetchIntraDayChartQuotes(
              $id: ID!, $date: Date, $tradingSession: TradingSession,
              $currency: Currency, $period: ChartPeriod
            ) {
              security(id: $id) {
                id
                chartBarQuotes(date: $date tradingSession: $tradingSession currency: $currency period: $period) {
                  securityId price sessionPrice timestamp currency marketStatus __typename
                }
                __typename
              }
            }
            """
            variables = {
                "id": security_id, "date": date,
                "tradingSession": trading_session, "currency": currency, "period": period,
            }
        else:
            gql_query = """
            query FetchIntraDayChartQuotes(
              $id: ID!, $date: Date, $currency: Currency, $period: ChartPeriod
            ) {
              security(id: $id) {
                id
                chartBarQuotes(date: $date currency: $currency period: $period) {
                  securityId price sessionPrice timestamp currency marketStatus __typename
                }
                __typename
              }
            }
            """
            variables = {"id": security_id, "date": date, "currency": currency, "period": period}

        variables = {k: v for k, v in variables.items() if v is not None}
        result = self.graphql_query("FetchIntraDayChartQuotes", gql_query, variables)
        return result.get('data', {}).get('security', {}).get('chartBarQuotes', [])

    def get_ticker_id(self, ticker: str, exchange: Optional[str] = None) -> Optional[str]:
        """Get security ID by ticker symbol."""
        results = self.search_securities(ticker)
        for result in results:
            stock = result.get('stock', {})
            if stock.get('symbol') == ticker:
                if exchange is None or stock.get('primaryExchange') == exchange:
                    return result.get('id')
        return None

    def parse_ticker_market(self, ticker_market: str) -> tuple:
        """Parse a ticker with market suffix into (symbol, market)."""
        if '.' in ticker_market:
            parts = ticker_market.rsplit('.', 1)
            return parts[0].upper(), parts[1].upper()
        return ticker_market.upper(), None

    def _is_us_exchange(self, exchange: str) -> bool:
        if not exchange:
            return False
        return exchange.upper() in {'NYSE', 'NASDAQ', 'AMEX', 'BATS', 'XNYS', 'XNAS', 'XASE'}

    def _is_canadian_exchange(self, exchange: str) -> bool:
        if not exchange:
            return False
        return exchange.upper() in {'TSX', 'TSXV', 'CSE', 'NEOE', 'XTSE', 'XTSX', 'XCSE'}

    def resolve_security_id(self, ticker_market: str) -> Optional[str]:
        """Resolve a TICKER.MARKET string to its Wealthsimple security ID."""
        if hasattr(self, '_security_id_cache') and ticker_market in self._security_id_cache:
            return self._security_id_cache[ticker_market]

        symbol, market = self.parse_ticker_market(ticker_market)
        results = self.search_securities(symbol)
        if not results:
            return None

        if market == 'US':
            exchange_check = self._is_us_exchange
        elif market == 'TO':
            exchange_check = self._is_canadian_exchange
        else:
            exchange_check = None

        for result in results:
            stock = result.get('stock') or {}
            if stock.get('symbol') == symbol:
                if exchange_check is None or exchange_check(stock.get('primaryExchange', '') or ''):
                    security_id = result.get('id')
                    if not hasattr(self, '_security_id_cache'):
                        self._security_id_cache = {}
                    self._security_id_cache[ticker_market] = security_id
                    return security_id

        return None

    # ==================== Options Trading ====================

    def get_option_chain(self, security_id: str, expiry_date: str, option_type: str = 'CALL',
                         include_greeks: bool = True, real_time_quote: bool = True,
                         first: Optional[int] = None, cursor: Optional[str] = None) -> List[Dict]:
        """Get option chain for a security."""
        gql_query = """
        query FetchOptionChain($id: ID!, $expiryDate: Date!, $optionType: OptionType!, $realTimeQuote: Boolean, $cursor: String, $first: Int, $includeGreeks: Boolean!) {
          security(id: $id) {
            id
            optionChain(expiryDate: $expiryDate optionType: $optionType realTimeQuote: $realTimeQuote first: $first after: $cursor) {
              edges {
                node { ...OptionChainSecurity __typename }
                __typename
              }
              pageInfo { hasNextPage endCursor __typename }
              __typename
            }
            __typename
          }
        }

        fragment OptionChainSecurity on Security {
          id
          ...OptionDetailsSummary
          quoteV2(currency: null) { ...SecurityQuoteV2 __typename }
          __typename
        }

        fragment OptionDetailsSummary on Security {
          optionDetails {
            strikePrice
            optionType
            greekSymbols @include(if: $includeGreeks) { ...OptionGreekSymbols __typename }
            __typename
          }
          __typename
        }

        fragment OptionGreekSymbols on OptionGreekSymbols {
          id rho vega delta theta gamma impliedVolatility calculationTime __typename
        }

        fragment StreamedSecurityQuoteV2 on UnifiedQuote {
          __typename securityId ask bid currency price sessionPrice quotedAsOf
          ... on EquityQuote {
            marketStatus askSize bidSize close high last lastSize low open mid volume: vol referenceClose __typename
          }
          ... on OptionQuote {
            marketStatus askSize bidSize close high last lastSize low open mid volume: vol
            breakEven inTheMoney liquidityStatus openInterest underlyingSpot __typename
          }
        }

        fragment SecurityQuoteV2 on UnifiedQuote {
          ...StreamedSecurityQuoteV2
          previousBaseline __typename
        }
        """
        variables = {
            "id": security_id,
            "expiryDate": expiry_date,
            "optionType": option_type,
            "realTimeQuote": real_time_quote,
            "cursor": cursor,
            "first": first,
            "includeGreeks": include_greeks
        }
        result = self.graphql_query("FetchOptionChain", gql_query, variables)
        edges = result.get('data', {}).get('security', {}).get('optionChain', {}).get('edges', [])
        return [edge.get('node', {}) for edge in edges]

    def get_option_expiry_dates(self, security_id: str, min_date: Optional[str] = None,
                                max_date: Optional[str] = None) -> List[str]:
        """Get available option expiry dates for a security."""
        if not min_date:
            min_date = datetime.now().strftime('%Y-%m-%d')
        if not max_date:
            max_date = (datetime.now() + timedelta(days=1095)).strftime('%Y-%m-%d')

        gql_query = """
        query FetchOptionExpirationDates($securityId: ID!, $minDate: Date!, $maxDate: Date!) {
          security(id: $securityId) {
            id
            optionExpirationDates(minDate: $minDate, maxDate: $maxDate) {
              ...OptionExpirationDates
              __typename
            }
            __typename
          }
        }

        fragment OptionExpirationDates on OptionExpirationDates {
          expirationDates
          __typename
        }
        """
        variables = {"securityId": security_id, "minDate": min_date, "maxDate": max_date}
        result = self.graphql_query("FetchOptionExpirationDates", gql_query, variables)
        option_dates = result.get('data', {}).get('security', {}).get('optionExpirationDates', {})
        return option_dates.get('expirationDates', [])

    def get_option_transaction_fees(self, side: str, premium: float, quantity: int,
                                    multiplier: int = 100, currency: str = 'CAD') -> Dict:
        """Calculate option transaction fees."""
        gql_query = """
        query FetchOptionTransactionFees($side: OrderType!, $premium: BigDecimal!,
                                        $quantity: Int!, $multiplier: Int!, $currency: Currency!) {
          optionTransactionFees(side: $side premium: $premium quantity: $quantity multiplier: $multiplier currency: $currency) {
            commission { amount currency }
            sec { amount currency }
            total { amount currency }
          }
        }
        """
        variables = {
            "side": side,
            "premium": str(premium),
            "quantity": quantity,
            "multiplier": multiplier,
            "currency": currency
        }
        result = self.graphql_query("FetchOptionTransactionFees", gql_query, variables)
        return result.get('data', {}).get('optionTransactionFees', {})

    # ==================== Account Management ====================

    def get_accounts(self, identity_id: Optional[str] = None) -> List[Dict]:
        """Get all accounts for the authenticated user."""
        identity_id = identity_id or self.identity_id
        if not identity_id:
            self._fetch_identity_id_from_token()
            identity_id = self.identity_id
        if not identity_id:
            raise Exception("No identity ID available. Please authenticate first.")

        gql_query = """
        query FetchAllAccounts($identityId: ID!, $filter: AccountsFilter = {}, $pageSize: Int = 25) {
          identity(id: $identityId) {
            id
            accounts(filter: $filter, first: $pageSize) {
              edges {
                node {
                  id branch currency nickname status unifiedAccountType type createdAt
                  custodianAccounts { id branch custodian status }
                  accountFeatures { name enabled functional }
                }
              }
            }
          }
        }
        """
        variables = {"identityId": identity_id, "filter": {}, "pageSize": 100}
        result = self.graphql_query("FetchAllAccounts", gql_query, variables)
        edges = result.get('data', {}).get('identity', {}).get('accounts', {}).get('edges', [])
        return [edge.get('node', {}) for edge in edges]

    def get_account_funding_balances(self, account_ids: List[str]) -> List[Dict]:
        """Get account funding balances (available trading cash)."""
        gql_query = """
        query FetchAccountFundingBalances($accountIds: [ID!]!) {
          account_funding_balances(account_ids: $accountIds) {
            ...AccountFundingBalance
            __typename
          }
        }

        fragment AccountFundingBalance on AccountFundingBalance {
          id
          trading_balances { amount currency __typename }
          __typename
        }
        """
        result = self.graphql_query("FetchAccountFundingBalances", gql_query, {"accountIds": account_ids})
        return result.get('data', {}).get('account_funding_balances', [])

    def create_internal_transfer(self, source_account_id: str, destination_account_id: str,
                                 amount: float, currency: str = 'CAD') -> Dict:
        """Transfer money between Wealthsimple accounts."""
        gql_query = """
        mutation FundingIntentInternalTransferCreate($input: CreateFundingIntentInternalTransferInput!) {
          createFundingIntentInternalTransfer: create_funding_intent_internal_transfer(input: $input) {
            ... on FundingIntent { id __typename }
            __typename
          }
        }
        """
        variables = {
            "input": {
                "source": {"id": source_account_id, "type": "Account"},
                "source_currency": currency.upper(),
                "destination": {"id": destination_account_id, "type": "Account"},
                "destination_currency": currency.upper(),
                "requested_amount_value": str(amount),
                "requested_amount_unit": "QUANTITY",
                "product_attribution": "simple_mm_web",
                "idempotency_key": str(uuid.uuid4())
            }
        }
        result = self.graphql_query("FundingIntentInternalTransferCreate", gql_query, variables)
        return result.get('data', {}).get('createFundingIntentInternalTransfer', {})

    def get_account_financials(self, account_ids: List[str], currency: str = 'CAD',
                               start_date: Optional[str] = None) -> List[Dict]:
        """Get financial information for specific accounts."""
        gql_query = """
        query FetchAccountFinancials($ids: [String!]!, $startDate: Date, $currency: Currency) {
          accounts(ids: $ids) {
            id
            ...AccountFinancials
            __typename
          }
        }

        fragment AccountFinancials on Account {
          id
          custodianAccounts {
            id branch
            financials { current { ...CustodianAccountCurrentFinancialValues __typename } __typename }
            __typename
          }
          financials {
            currentCombined(currency: $currency) { id ...AccountCurrentFinancials __typename }
            __typename
          }
          __typename
        }

        fragment CustodianAccountCurrentFinancialValues on CustodianAccountCurrentFinancialValues {
          deposits { ...Money __typename }
          earnings { ...Money __typename }
          netDeposits { ...Money __typename }
          netLiquidationValue { ...Money __typename }
          withdrawals { ...Money __typename }
          __typename
        }

        fragment Money on Money {
          amount cents currency __typename
        }

        fragment AccountCurrentFinancials on AccountCurrentFinancials {
          id
          netLiquidationValueV2 { ...Money __typename }
          netDeposits: netDepositsV2 { ...Money __typename }
          simpleReturns(referenceDate: $startDate) { ...SimpleReturns __typename }
          totalDeposits: totalDepositsV2 { ...Money __typename }
          totalWithdrawals: totalWithdrawalsV2 { ...Money __typename }
          __typename
        }

        fragment SimpleReturns on SimpleReturns {
          amount { ...Money __typename }
          asOf rate referenceDate __typename
        }
        """
        variables = {"ids": account_ids, "currency": currency, "startDate": start_date}
        result = self.graphql_query("FetchAccountFinancials", gql_query, variables)
        return result.get('data', {}).get('accounts', [])

    def get_account_current_financials(self, account_id: str, currency: str = 'CAD',
                                       start_date: Optional[str] = None) -> Dict:
        """Get current financial metrics for a specific account."""
        gql_query = """
        query FetchAccountCurrentFinancials($id: ID!, $currency: Currency, $startDate: Date) {
          account(id: $id) {
            id
            financials {
              current(currency: $currency) {
                id
                netLiquidationValueV2 { amount cents currency }
                netDeposits: netDepositsV2 { amount cents currency }
                simpleReturns(referenceDate: $startDate) {
                  amount { amount cents currency }
                  asOf rate referenceDate
                }
                totalDeposits: totalDepositsV2 { amount cents currency }
                totalWithdrawals: totalWithdrawalsV2 { amount cents currency }
                __typename
              }
              __typename
            }
            __typename
          }
        }
        """
        variables = {k: v for k, v in {"id": account_id, "currency": currency, "startDate": start_date}.items() if v is not None}
        result = self.graphql_query("FetchAccountCurrentFinancials", gql_query, variables)
        return result.get('data', {}).get('account', {}).get('financials', {}).get('current', {})

    def get_account_graph_data(
        self,
        account_id: str,
        currency: str = 'CAD',
        time_range: str = 'ONE_DAY',
        market_session: str = 'REGULAR',
        include_simple_returns: bool = False,
    ) -> Dict:
        """Get account graph data (portfolio value over time)."""
        gql_query = """
        query FetchAccountGraphData(
          $id: ID!, $currency: Currency!, $timeRange: GraphTimeRange!,
          $marketSession: GraphMarketSession!, $includeSimpleReturns: Boolean = false
        ) {
          account(id: $id) {
            id
            financials {
              currentCombined(currency: $currency) {
                id
                graphData(timeRange: $timeRange, marketSession: $marketSession) {
                  previousClose {
                    dateTime
                    netLiquidationValue { amount currency __typename }
                    __typename
                  }
                  data {
                    dateTime
                    netLiquidationValue { amount currency __typename }
                    netDeposits { amount currency __typename }
                    simpleReturns @include(if: $includeSimpleReturns) {
                      amount { amount currency __typename }
                      rate referenceDateTime __typename
                    }
                    __typename
                  }
                  __typename
                }
                __typename
              }
              __typename
            }
            __typename
          }
        }
        """
        variables = {
            "id": account_id, "currency": currency, "timeRange": time_range,
            "marketSession": market_session, "includeSimpleReturns": include_simple_returns,
        }
        result = self.graphql_query("FetchAccountGraphData", gql_query, variables)
        current = (result.get('data', {}).get('account', {})
                   .get('financials', {}).get('currentCombined', {}))
        return current.get('graphData', {})

    def get_identity_historical_financials(self, identity_id: Optional[str] = None, currency: str = 'CAD',
                                           start_date: Optional[str] = None, end_date: Optional[str] = None,
                                           account_ids: Optional[List[str]] = None,
                                           account_scope: str = 'OWN',
                                           include_simple_returns: bool = False,
                                           first: Optional[int] = None, cursor: Optional[str] = None) -> Dict:
        """Get daily historical financial data for an identity (portfolio value over time)."""
        identity_id = identity_id or self.identity_id
        if not identity_id:
            self._fetch_identity_id_from_token()
            identity_id = self.identity_id
        if not identity_id:
            raise Exception("No identity ID available. Please authenticate first.")

        gql_query = """
        query FetchIdentityHistoricalFinancials($identityId: ID!, $currency: Currency!, $startDate: Date, $endDate: Date, $first: Int, $cursor: String, $accountIds: [ID!], $includeSimpleReturns: Boolean = false, $accountScope: AccountScope = OWN) {
          identity(id: $identityId) {
            id
            financials(filter: {accounts: $accountIds}, accountScope: $accountScope) {
              historicalDaily(currency: $currency startDate: $startDate endDate: $endDate first: $first after: $cursor) {
                edges {
                  node { ...IdentityHistoricalFinancials __typename }
                  __typename
                }
                pageInfo { hasNextPage endCursor __typename }
                __typename
              }
              __typename
            }
            __typename
          }
        }

        fragment IdentityHistoricalFinancials on IdentityHistoricalDailyFinancials {
          date
          netLiquidationValueV2 { amount currency __typename }
          netDepositsV2 { amount currency __typename }
          simpleReturns(referenceDate: $startDate) @include(if: $includeSimpleReturns) { ...SimpleReturns __typename }
          __typename
        }

        fragment SimpleReturns on SimpleReturns {
          amount { ...Money __typename }
          asOf rate referenceDate __typename
        }

        fragment Money on Money {
          amount cents currency __typename
        }
        """
        variables = {
            "identityId": identity_id,
            "currency": currency,
            "includeSimpleReturns": include_simple_returns,
            "accountScope": account_scope,
        }
        if start_date is not None:
            variables["startDate"] = start_date
        if end_date is not None:
            variables["endDate"] = end_date
        if account_ids is not None:
            variables["accountIds"] = account_ids
        if first is not None:
            variables["first"] = first
        if cursor is not None:
            variables["cursor"] = cursor

        result = self.graphql_query("FetchIdentityHistoricalFinancials", gql_query, variables)
        return result.get('data', {}).get('identity', {}).get('financials', {}).get('historicalDaily', {})

    def get_positions(self, identity_id: Optional[str] = None, account_ids: Optional[List[str]] = None,
                      currency: Optional[str] = None, security_type: Optional[str] = None,
                      include_security: bool = True, first: int = 500, aggregated: bool = False) -> List[Dict]:
        """Get positions for the authenticated user."""
        if currency is None:
            currency_override = 'MARKET'
            currency = 'CAD'
        else:
            currency_override = None

        identity_id = identity_id or self.identity_id
        if not identity_id:
            self._fetch_identity_id_from_token()
            identity_id = self.identity_id
        if not identity_id:
            raise Exception("No identity ID available. Please authenticate first.")

        gql_query = """
        query FetchIdentityPositions($identityId: ID!, $currency: Currency!, $first: Int, $cursor: String,
                                     $accountIds: [ID!], $aggregated: Boolean, $currencyOverride: CurrencyOverride,
                                     $filter: PositionFilter, $includeSecurity: Boolean = false) {
          identity(id: $identityId) {
            id
            financials(filter: {accounts: $accountIds}) {
              current(currency: $currency) {
                id
                positions(first: $first, after: $cursor, aggregated: $aggregated, filter: $filter) {
                  edges {
                    node {
                      id quantity percentageOfAccount positionDirection
                      bookValue { amount currency __typename }
                      averagePrice { amount currency __typename }
                      marketAveragePrice: averagePrice(currencyOverride: $currencyOverride) { amount currency __typename }
                      marketBookValue: bookValue(currencyOverride: $currencyOverride) { amount currency __typename }
                      totalValue(currencyOverride: $currencyOverride) { amount currency __typename }
                      unrealizedReturns { amount currency __typename }
                      marketUnrealizedReturns: unrealizedReturns(currencyOverride: $currencyOverride) { amount currency __typename }
                      security {
                        id securityType currency status logoUrl features
                        stock @include(if: $includeSecurity) {
                          name symbol primaryExchange primaryMic __typename
                        }
                        optionDetails @include(if: $includeSecurity) {
                          strikePrice optionType expiryDate osiSymbol multiplier maturity
                          underlyingSecurity {
                            id
                            stock { name symbol primaryExchange __typename }
                            __typename
                          }
                          __typename
                        }
                        quoteV2(currency: null) @include(if: $includeSecurity) {
                          securityId currency price sessionPrice ask bid quotedAsOf previousBaseline __typename
                        }
                        __typename
                      }
                      __typename
                    }
                    __typename
                  }
                  pageInfo { hasNextPage endCursor __typename }
                  totalCount status __typename
                }
                __typename
              }
              __typename
            }
            __typename
          }
        }
        """
        position_filter = {}
        if security_type:
            position_filter['positionSecurityType'] = security_type

        variables = {
            "identityId": identity_id,
            "currency": currency,
            "currencyOverride": currency_override,
            "accountIds": account_ids,
            "filter": position_filter if position_filter else None,
            "first": first,
            "aggregated": aggregated,
            "includeSecurity": include_security,
            "cursor": None
        }
        result = self.graphql_query("FetchIdentityPositions", gql_query, variables)
        positions_data = (result.get('data', {}).get('identity', {})
                          .get('financials', {}).get('current', {}).get('positions', {}))
        return [edge.get('node', {}) for edge in positions_data.get('edges', [])]

    def get_activities(self, account_ids: Optional[List[str]] = None, types: Optional[List[str]] = None,
                       statuses: Optional[List[str]] = None, sub_types: Optional[List[str]] = None,
                       security_ids: Optional[List[str]] = None, start_date: Optional[str] = None,
                       end_date: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None) -> Dict:
        """Get activity feed items (orders, trades, deposits, etc.)."""
        gql_query = """
        query FetchActivityFeedItems($first: Int, $cursor: Cursor, $condition: ActivityCondition,
                                     $orderBy: [ActivitiesOrderBy!] = OCCURRED_AT_DESC) {
          activityFeedItems(first: $first, after: $cursor, condition: $condition, orderBy: $orderBy) {
            edges {
              node { ...Activity __typename }
              __typename
            }
            pageInfo { hasNextPage endCursor __typename }
            __typename
          }
        }

        fragment Activity on ActivityFeedItem {
          accountId aftOriginatorName aftTransactionCategory aftTransactionType
          amount amountSign assetQuantity assetSymbol canonicalId currency
          eTransferEmail eTransferName externalCanonicalId groupId identityId
          institutionName occurredAt p2pHandle p2pMessage spendMerchant securityId
          billPayCompanyName billPayPayeeNickname redactedExternalAccountNumber
          opposingAccountId status subType type strikePrice contractType expiryDate
          chequeNumber provisionalCreditAmount primaryBlocker interestRate frequency
          counterAssetSymbol rewardProgram counterPartyCurrency counterPartyCurrencyAmount
          counterPartyName fxRate fees reference transferType optionStrategy
          rejectionReason resolvable __typename
        }
        """
        condition = {}
        if account_ids:
            condition['accountIds'] = account_ids
        if types:
            condition['types'] = types
        if statuses:
            condition['unifiedStatuses'] = statuses
        if sub_types:
            condition['subTypes'] = sub_types
        if security_ids:
            condition['securityIds'] = security_ids
        if start_date:
            condition['startDate'] = start_date
        if end_date:
            condition['endDate'] = end_date

        variables = {
            "first": limit,
            "cursor": cursor,
            "condition": condition if condition else None,
            "orderBy": "OCCURRED_AT_DESC"
        }
        result = self.graphql_query("FetchActivityFeedItems", gql_query, variables)
        activity_data = result.get('data', {}).get('activityFeedItems', {})
        return {
            'items': [edge.get('node', {}) for edge in activity_data.get('edges', [])],
            'pageInfo': activity_data.get('pageInfo', {})
        }

    def get_pending_orders(self, account_ids: Optional[List[str]] = None) -> List[Dict]:
        """Get all pending orders for specified accounts."""
        order_types = [
            'MANAGED_BUY', 'CRYPTO_BUY', 'DIY_BUY', 'OPTIONS_BUY',
            'MANAGED_SELL', 'CRYPTO_SELL', 'DIY_SELL', 'OPTIONS_SELL', 'OPTIONS_MULTILEG'
        ]
        order_subtypes = ['FRACTIONAL_ORDER', 'MARKET_ORDER', 'STOP_ORDER', 'LIMIT_ORDER', 'STOP_LIMIT_ORDER']
        result = self.get_activities(
            account_ids=account_ids, types=order_types, statuses=['PENDING'],
            sub_types=order_subtypes, limit=100
        )
        return result['items']

    def get_security_activities(self, security_id: str, account_ids: Optional[List[str]] = None,
                                start_date: Optional[str] = None, end_date: Optional[str] = None,
                                limit: int = 100) -> List[Dict]:
        """Get all activities for a specific security."""
        result = self.get_activities(
            account_ids=account_ids, security_ids=[security_id],
            start_date=start_date, end_date=end_date, limit=limit
        )
        return result['items']

    # ==================== Trading ====================

    def create_order(self, account_id: str, security_id: str, quantity: int,
                     order_type: str = 'BUY_QUANTITY', execution_type: str = 'LIMIT',
                     limit_price: Optional[float] = None, stop_price: Optional[float] = None,
                     time_in_force: str = 'DAY', open_close: Optional[str] = None,
                     trading_session: Optional[str] = None) -> Dict:
        """Create a new order (stock or option)."""
        gql_query = """
        mutation SoOrdersOrderCreate($input: SoOrders_CreateOrderInput!) {
          soOrdersCreateOrder(input: $input) {
            errors { code message __typename }
            order { orderId createdAt __typename }
            __typename
          }
        }
        """
        order_input = {
            "canonicalAccountId": account_id,
            "externalId": f"order-{uuid.uuid4()}",
            "executionType": execution_type,
            "orderType": order_type,
            "quantity": quantity,
            "securityId": security_id,
            "timeInForce": time_in_force
        }
        if limit_price is not None:
            order_input["limitPrice"] = limit_price
        if stop_price is not None:
            order_input["stopPrice"] = stop_price
        if open_close is not None:
            order_input["openClose"] = open_close
        if trading_session is not None:
            order_input["tradingSession"] = trading_session

        result = self.graphql_query("SoOrdersOrderCreate", gql_query, {"input": order_input})
        return result.get('data', {}).get('soOrdersCreateOrder', {})

    def market_buy(self, account_id: str, security_id: str, quantity: int) -> Dict:
        """Place a market buy order."""
        return self.create_order(account_id, security_id, quantity, 'BUY_QUANTITY', 'MARKET')

    def market_sell(self, account_id: str, security_id: str, quantity: int) -> Dict:
        """Place a market sell order."""
        return self.create_order(account_id, security_id, quantity, 'SELL_QUANTITY', 'MARKET')

    def limit_buy(self, account_id: str, security_id: str, quantity: int, limit_price: float) -> Dict:
        """Place a limit buy order."""
        return self.create_order(account_id, security_id, quantity, 'BUY_QUANTITY', 'LIMIT', limit_price=limit_price)

    def limit_sell(self, account_id: str, security_id: str, quantity: int, limit_price: float) -> Dict:
        """Place a limit sell order."""
        return self.create_order(account_id, security_id, quantity, 'SELL_QUANTITY', 'LIMIT', limit_price=limit_price)

    def stop_limit_buy(self, account_id: str, security_id: str, quantity: int,
                       limit_price: float, stop_price: float) -> Dict:
        """Place a stop-limit buy order."""
        return self.create_order(account_id, security_id, quantity, 'BUY_QUANTITY', 'STOP_LIMIT',
                                 limit_price=limit_price, stop_price=stop_price)

    def stop_limit_sell(self, account_id: str, security_id: str, quantity: int,
                        limit_price: float, stop_price: float) -> Dict:
        """Place a stop-limit sell order."""
        return self.create_order(account_id, security_id, quantity, 'SELL_QUANTITY', 'STOP_LIMIT',
                                 limit_price=limit_price, stop_price=stop_price)

    def buy_option(self, account_id: str, option_id: str, quantity: int, limit_price: float,
                   open_close: str = 'OPEN') -> Dict:
        """Buy an option contract."""
        return self.create_order(account_id, option_id, quantity, 'BUY_QUANTITY', 'LIMIT',
                                 limit_price=limit_price, open_close=open_close)

    def sell_option(self, account_id: str, option_id: str, quantity: int, limit_price: float,
                    open_close: str = 'CLOSE') -> Dict:
        """Sell an option contract."""
        return self.create_order(account_id, option_id, quantity, 'SELL_QUANTITY', 'LIMIT',
                                 limit_price=limit_price, open_close=open_close)

    def stop_limit_sell_option(self, account_id: str, option_id: str, quantity: int,
                               limit_price: float, stop_price: float, open_close: str = 'CLOSE') -> Dict:
        """Place a stop-limit sell order for an option contract."""
        return self.create_order(account_id, option_id, quantity, 'SELL_QUANTITY', 'STOP_LIMIT',
                                 limit_price=limit_price, stop_price=stop_price, open_close=open_close)

    def cancel_order(self, external_id: str) -> Dict:
        """Cancel an existing order."""
        gql_query = """
        mutation SoOrdersOrderCancel($cancelOrderRequest: CancelOrderRequest!) {
          orderServiceCancelOrder(cancelOrderRequest: $cancelOrderRequest) {
            externalId
            errors { code message __typename }
            __typename
          }
        }
        """
        result = self.graphql_query("SoOrdersOrderCancel", gql_query,
                                    {"cancelOrderRequest": {"externalId": external_id}})
        cancel_response = result.get('data', {}).get('orderServiceCancelOrder', {})
        if cancel_response.get('errors'):
            errors = cancel_response['errors']
            error_messages = [f"{e.get('code', 'UNKNOWN')}: {e.get('message', 'No message')}" for e in errors]
            raise Exception(f"Failed to cancel order: {'; '.join(error_messages)}")
        return cancel_response

    def get_extended_order(self, external_id: str, branch_id: str = 'TR') -> Dict:
        """Get extended order details including fill information and status."""
        gql_query = """
        query FetchSoOrdersExtendedOrder($branchId: String!, $externalId: String!) {
          soOrdersExtendedOrder(branchId: $branchId, externalId: $externalId) {
            ...SoOrdersExtendedOrder
            __typename
          }
        }

        fragment SoOrdersExtendedOrder on SoOrders_ExtendedOrderResponse {
          averageFilledPrice filledExchangeRate filledQuantity filledCommissionFee filledTotalFee
          firstFilledAtUtc lastFilledAtUtc limitPrice openClose orderType optionMultiplier
          rejectionCause rejectionCode securityCurrency status stopPrice submittedAtUtc
          submittedExchangeRate submittedNetValue submittedQuantity submittedTotalFee
          timeInForce accountId canonicalAccountId cancellationCutoff tradingSession expiredAtUtc
          __typename
        }
        """
        result = self.graphql_query("FetchSoOrdersExtendedOrder", gql_query,
                                    {"branchId": branch_id, "externalId": external_id})
        return result.get('data', {}).get('soOrdersExtendedOrder', {})

    # ==================== Identity & User Info ====================

    def get_identity(self, identity_id: Optional[str] = None) -> Dict:
        """Get identity information for the authenticated user."""
        identity_id = identity_id or self.identity_id
        if not identity_id:
            self._fetch_identity_id_from_token()
            identity_id = self.identity_id
        if not identity_id:
            raise Exception("No identity ID available. Please authenticate first.")

        gql_query = """
        query FetchIdentity($id: ID!) {
          identity(id: $id) {
            id createdAt email emailVerified fullName givenName familyName
            citizenship dateOfBirth phoneNumber
            address { streetAddress city province postalCode country }
          }
        }
        """
        result = self.graphql_query("FetchIdentity", gql_query, {"id": identity_id})
        return result.get('data', {}).get('identity', {})

    def subscribe(self, device_id: Optional[str] = None) -> 'WealthsimpleSubscriptions':
        """
        Create a WebSocket subscription client for real-time data streams.

        Example:
            async with ws.subscribe() as sub:
                async for msg in sub.stream_quotes(['sec-s-xxxxx']):
                    print(f"Quote update: {msg}")
        """
        if not WEBSOCKETS_AVAILABLE:
            raise Exception(
                "WebSocket support requires the 'websockets' library. "
                "Install it with: pip install websockets"
            )
        self._ensure_authenticated()
        return WealthsimpleSubscriptions(
            access_token=self.access_token,
            identity_id=self.identity_id,
            device_id=device_id
        )
