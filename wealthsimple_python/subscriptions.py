import json
import asyncio
import uuid
from typing import Dict, List, Optional, Any, AsyncIterator

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None
    WebSocketClientProtocol = None


class WealthsimpleSubscriptions:
    """
    WebSocket subscription client for Wealthsimple real-time data streams.

    Implements GraphQL subscriptions over WebSocket using the graphql-transport-ws
    protocol for real-time updates on quotes, activity, identity, and balance changes.

    Usage:
        async with ws.subscribe() as sub:
            async for msg in sub.stream_quotes(['sec-s-xxxxx']):
                print(f"Price: {msg['payload']['data']['securityQuoteUpdates']['quoteV2']['price']}")
    """

    def __init__(self, access_token: str, identity_id: Optional[str] = None,
                 device_id: Optional[str] = None):
        if not WEBSOCKETS_AVAILABLE:
            raise Exception(
                "WebSocket support requires the 'websockets' library. "
                "Install it with: pip install websockets"
            )

        self.access_token = access_token
        self.identity_id = identity_id
        self.device_id = device_id or uuid.uuid4().hex
        self.ws: Optional[WebSocketClientProtocol] = None
        self._connection_ack_event = asyncio.Event()
        self._subscriptions: Dict[str, asyncio.Queue] = {}
        self._receiver_task: Optional[asyncio.Task] = None

        self.candidate_urls = [
            "wss://realtime-api.wealthsimple.com/subscription",
            "wss://my.wealthsimple.com/graphql",
            "wss://my.wealthsimple.com/subscriptions",
            "wss://my.wealthsimple.com/subscription",
        ]

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Origin": "https://my.wealthsimple.com",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
            ),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Authorization": f"Bearer {self.access_token}",
            "x-ws-api-version": "12",
            "x-platform-os": "web",
            "x-ws-locale": "en-CA",
            "x-ws-profile": "trade",
        }

    async def connect(self) -> None:
        headers = self._get_headers()
        last_exc = None

        for url in self.candidate_urls:
            try:
                try:
                    self.ws = await websockets.connect(
                        url,
                        additional_headers=headers,
                        subprotocols=["graphql-transport-ws"],
                        max_size=None,
                        open_timeout=20,
                        close_timeout=10,
                    )
                except TypeError:
                    self.ws = await websockets.connect(
                        url,
                        extra_headers=headers,
                        subprotocols=["graphql-transport-ws"],
                        max_size=None,
                        open_timeout=20,
                        close_timeout=10,
                    )
                break
            except Exception as e:
                last_exc = e
                continue

        if self.ws is None:
            raise last_exc or RuntimeError("Failed to establish WebSocket connection")

        init_payload = {
            "Authorization": f"Bearer {self.access_token}",
            "x-ws-api-version": "12",
            "x-ws-locale": "en-CA",
            "x-ws-profile": "trade",
            "x-platform-os": "web",
            "x-ws-device-id": self.device_id,
        }
        await self._send_message({"type": "connection_init", "payload": init_payload})

        self._receiver_task = asyncio.create_task(self._receiver())

        try:
            await asyncio.wait_for(self._connection_ack_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass

        if self.ws:
            try:
                await self.ws.close(code=1000, reason="client shutdown")
            except Exception:
                pass
            self.ws = None

    async def _send_message(self, message: Dict[str, Any]) -> None:
        if not self.ws:
            raise Exception("WebSocket not connected")
        await self.ws.send(json.dumps(message))

    async def _receiver(self) -> None:
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type", "unknown")

                    if msg_type == "connection_ack":
                        self._connection_ack_event.set()
                    elif msg_type in ("next", "error"):
                        sub_id = data.get("id")
                        if sub_id and sub_id in self._subscriptions:
                            await self._subscriptions[sub_id].put(data)
                    elif msg_type == "complete":
                        sub_id = data.get("id")
                        if sub_id and sub_id in self._subscriptions:
                            await self._subscriptions[sub_id].put(None)
                except (json.JSONDecodeError, Exception):
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _subscribe(self, operation_name: str, query: str,
                         variables: Optional[Dict] = None) -> AsyncIterator[Dict]:
        sub_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        self._subscriptions[sub_id] = queue

        try:
            await self._send_message({
                "id": sub_id,
                "type": "subscribe",
                "payload": {
                    "operationName": operation_name,
                    "query": query,
                    "variables": variables or {}
                }
            })

            while True:
                msg = await queue.get()
                if msg is None:
                    break
                yield msg
        finally:
            self._subscriptions.pop(sub_id, None)

    async def stream_quotes(self, security_ids: List[str],
                            currency: Optional[str] = None) -> AsyncIterator[Dict]:
        """Stream real-time quote updates for one or more securities."""
        query = """
        subscription QuoteV2BySecurityIdStream($id: ID!, $currency: Currency = null) {
          securityQuoteUpdates(id: $id) {
            id
            quoteV2(currency: $currency) {
              __typename
              securityId
              ask
              bid
              currency
              price
              sessionPrice
              quotedAsOf
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
                __typename
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
                __typename
              }
            }
            __typename
          }
        }
        """

        async def stream_single(security_id: str):
            async for msg in self._subscribe(
                "QuoteV2BySecurityIdStream",
                query,
                {"id": security_id, "currency": currency}
            ):
                yield msg

        if len(security_ids) == 1:
            async for msg in stream_single(security_ids[0]):
                yield msg
        else:
            for sid in security_ids:
                async for msg in stream_single(sid):
                    yield msg

    async def stream_activity_updates(self) -> AsyncIterator[Dict]:
        """Stream activity feed updates."""
        query = """
        subscription ActivityFeedUpdate {
          activityFeedUpdates {
            accountId
            activityId
            updatedAt
            __typename
          }
        }
        """
        async for msg in self._subscribe("ActivityFeedUpdate", query):
            yield msg

    async def stream_identity_updates(self, identity_id: Optional[str] = None) -> AsyncIterator[Dict]:
        """Stream identity and account core updates."""
        identity_id = identity_id or self.identity_id
        if not identity_id:
            raise Exception("identity_id is required for identity updates subscription")

        query = """
        subscription IdentityAccountCoreUpdates($identityId: ID!) {
          identityAccountCoreUpdates(identityId: $identityId) {
            __typename
            ... on AccountUpdate {
              id
              eventName
              __typename
            }
            ... on IdentityUpdate {
              id
              eventName
              __typename
            }
          }
        }
        """
        async for msg in self._subscribe(
            "IdentityAccountCoreUpdates",
            query,
            {"identityId": identity_id}
        ):
            yield msg

    async def stream_balance_changes(self, custodian_account_ids: List[str]) -> AsyncIterator[Dict]:
        """Stream custodian account cash balance changes."""
        query = """
        subscription CustodianAccountBalanceChanges($custodianAccountIds: [ID!]!) {
          custodianAccountCashBalanceChanges(custodianAccountIds: $custodianAccountIds) {
            id
            __typename
          }
        }
        """
        async for msg in self._subscribe(
            "CustodianAccountBalanceChanges",
            query,
            {"custodianAccountIds": custodian_account_ids}
        ):
            yield msg

    async def ping(self) -> None:
        """Send a ping to keep the connection alive."""
        await self._send_message({"type": "ping", "payload": {}})
