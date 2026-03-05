"""
logic/crypto_agent.py — 加密貨幣交易員 (Crypto Agent)

職責：
1. 透過 ccxt 與各大加密貨幣交易所 (OKX, Bybit, Binance 等) 互動
2. 查詢帳戶餘額 (get_balance)
3. 獲取即時價格 (get_price)
4. 執行下單 (place_order)
"""

import os
import logging
from logic.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

class CryptoAgent:
    def __init__(self, registry: AgentRegistry):
        self._registry = registry
        self.exchange_id = os.getenv("CRYPTO_EXCHANGE", "binance").lower()
        self.api_key = os.getenv("CRYPTO_API_KEY", "")
        self.secret = os.getenv("CRYPTO_API_SECRET", "")
        self.password = os.getenv("CRYPTO_PASSPHRASE", "")  # OKX, KuCoin 等需要

        self.exchange = None

    async def close(self):
        """關閉交易所連線，釋放資源"""
        if self.exchange:
            await self.exchange.close()

    def _ensure_exchange(self) -> bool:
        """確保交易所已初始化 (Lazy Initialization)"""
        if self.exchange is not None:
            return True

        try:
            import ccxt.async_support as ccxt
        except ImportError:
            logger.error("❌ ccxt 套件未安裝。")
            return False

        if not hasattr(ccxt, self.exchange_id):
            logger.error(f"❌ 不支援的交易所：{self.exchange_id}")
            return False

        exchange_class = getattr(ccxt, self.exchange_id)
        
        config = {
            'apiKey': self.api_key,
            'secret': self.secret,
            'enableRateLimit': True,
        }
        
        # 某些交易所需要額外的 password/passphrase
        if self.password:
            config['password'] = self.password
            
        try:
            self.exchange = exchange_class(config)
            # 可選：若想測試是否能連線或使用沙盒環境，可在這裡設定
            # self.exchange.set_sandbox_mode(True) 
            return True
        except Exception as e:
            logger.error(f"❌ 初始化交易所失敗：{e}")
            return False

    async def get_balance(self) -> str:
        """查詢帳戶餘額"""
        if not self._ensure_exchange():
            return "❌ 交易所初始化失敗或未支援。"
            
        import ccxt.async_support as ccxt
        if not self.api_key:
            return "❌ 未設定正確的交易所 API Key。"

        try:
            balance = await self.exchange.fetch_balance()
            
            # 過濾出大於 0 的資產
            free_balance = balance.get('free', {})
            assets = {k: v for k, v in free_balance.items() if v > 0}
            
            if not assets:
                return f"🏦 目前在 {self.exchange_id.upper()} 帳戶中沒有任何可用餘額。"
                
            report = f"🏦 **{self.exchange_id.upper()} 帳戶餘額**\n"
            for coin, amount in assets.items():
                report += f"- {coin}: `{amount}`\n"
            return report

        except ccxt.AuthenticationError:
            return "❌ API Key 驗證失敗，請檢查金鑰或權限設定。"
        except Exception as e:
            logger.error(f"查詢餘額時發生錯誤: {e}")
            return f"❌ 查詢餘額時發生錯誤: {str(e)}"

    async def get_price(self, symbol: str) -> str:
        """獲取即時價格"""
        if not self._ensure_exchange():
            return "❌ 交易所初始化失敗或未支援。"
            
        import ccxt.async_support as ccxt
            
        symbol = symbol.upper()
        # CCXT 通常使用 BTC/USDT 格式，若用戶輸入 BTCUSDT，需嘗試轉換
        if '/' not in symbol and symbol.endswith('USDT'):
            symbol = symbol.replace('USDT', '/USDT')

        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            current_price = ticker.get('last')
            if current_price:
                return f"📈 **{symbol}** 目前價格：`{current_price}`"
            else:
                return f"⚠️ 無法獲取 {symbol} 的最新價格。"
        except ccxt.BadSymbol:
            return f"❌ 找不到交易對 {symbol}，請確認幣種名稱是否正確（例如：BTC/USDT）。"
        except Exception as e:
            logger.error(f"查詢價格時發生錯誤: {e}")
            return f"❌ 查詢價格時發生錯誤: {str(e)}"

    async def place_order(self, symbol: str, side: str, order_type: str, amount: float, price: float | None = None) -> str:
        """執行下單"""
        if not self._ensure_exchange():
            return "❌ 交易所初始化失敗或未支援。"
            
        import ccxt.async_support as ccxt
        if not self.api_key:
            return "❌ 未設定正確的交易所 API Key。"

        symbol = symbol.upper()
        if '/' not in symbol and symbol.endswith('USDT'):
            symbol = symbol.replace('USDT', '/USDT')
            
        side = side.lower()
        if side not in ['buy', 'sell']:
            return "❌ 交易方向錯誤，請指定 'buy' (買入) 或 'sell' (賣出)。"
            
        order_type = order_type.lower()
        if order_type not in ['market', 'limit']:
            return "❌ 訂單類型錯誤，請指定 'market' (市價) 或 'limit' (限價)。"
            
        if order_type == 'limit' and price is None:
            return "❌ 限價單必須提供價格 (price)。"

        try:
            if order_type == 'market':
                order = await self.exchange.create_market_order(symbol, side, amount)
            else:
                order = await self.exchange.create_limit_order(symbol, side, amount, price)
                
            status = order.get('status', 'unknown')
            filled = order.get('filled', 0)
            
            res = f"✅ **下單成功**\n"
            res += f"- 交易對：`{symbol}`\n"
            res += f"- 方向：`{side.upper()}`\n"
            res += f"- 類型：`{order_type.upper()}`\n"
            res += f"- 數量：`{amount}`\n"
            if price:
                res += f"- 價格：`{price}`\n"
            res += f"- 狀態：`{status}` (已成交: {filled})"
            return res

        except ccxt.InsufficientFunds:
            return "❌ 餘額不足，無法下單。"
        except ccxt.InvalidOrder as e:
            return f"❌ 無效的訂單參數：{e}"
        except Exception as e:
            logger.error(f"下單時發生錯誤: {e}")
            return f"❌ 下單時發生錯誤: {str(e)}"
