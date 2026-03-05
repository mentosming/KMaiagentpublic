import logging
from logic.agent_registry import AgentRegistry
from logic.agent_wallet import AgentWallet

logger = logging.getLogger(__name__)

class WalletAgent:
    def __init__(self, registry: AgentRegistry):
        self._registry = registry
        # 初始化會自動讀取或生成私鑰
        self.wallet = AgentWallet()


    async def get_address(self) -> str:
        """獲取目前的系統錢包地址"""
        res = f"📍 我的 Web3 錢包地址:\n"
        res += f"- **EVM (Base)**: `{self.wallet.address}`\n"
        res += f"- **Solana**: `{self.wallet.sol_address}`"
        return res

    async def get_balance(self, token_address: str = None, chain: str = "base") -> str:
        """獲取餘額，支援 Base (ETH/ERC20) 與 Solana (SOL/SPL)"""
        chain = chain.lower()
        
        # 自動推斷：如果 token_address 看起來像 Solana 地址（沒有 0x 開頭且長度約為 32-44 字元），自動切換到 solana
        if token_address and not token_address.startswith("0x") and 32 <= len(token_address) <= 44:
            chain = "solana"
        
        if chain == "solana":
            res = f"💰 **Agent Solana 錢包餘額** (`{self.wallet.sol_address}`)\n"
            sol_bal = self.wallet.get_sol_balance()
            res += f"🔹 **SOL**: `{sol_bal:.6f}`\n"
            
            if token_address:
                token_bal = self.wallet.get_spl_token_balance(token_address)
                res += f"🔹 **Token** (`{token_address}`): `{token_bal}`\n"
            else:
                # 預設查 Solana 上的 USDT (SPL)
                USDT_SOL = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
                usdt_bal = self.wallet.get_spl_token_balance(USDT_SOL)
                res += f"🔹 **USDT (Solana)**: `{usdt_bal:.2f}`\n"
            return res
            
        # 預設走 Base/EVM 邏輯
        res = f"💰 **Agent EVM 錢包餘額 (Base)** (`{self.wallet.address}`)\n"
        eth_bal = self.wallet.get_gas_balance()
        res += f"🔹 **ETH (Base)**: `{eth_bal:.6f}`\n"
        
        if token_address:
            token_bal = self.wallet.get_token_balance(token_address)
            res += f"🔹 **Token** (`{token_address}`): `{token_bal}`\n"
        else:
            # 預設查 USDC 與 USDT on Base
            USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
            # 由於有些用戶會問 USDT (Base 上雖然少，但也可查)
            USDT = "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb" # approximate base USDT address or often bridged
            
            usdc_bal = self.wallet.get_token_balance(USDC)
            res += f"🔹 **USDC (Base)**: `{usdc_bal:.2f}`\n"
            
            # USDT on Base network address usually is this one if bridged, but normally Base uses USDC.
            # We fetch anyway just in case the user specifically asks.
            try:
                usdt_bal = self.wallet.get_token_balance(USDT)
                res += f"🔹 **USDT (Base)**: `{usdt_bal:.2f}`\n"
            except Exception:
                pass
                
        return res

    async def swap(self, from_token: str, to_token: str, amount: float) -> str:
        """在 Solana 鏈上執行代幣兌換 (Jupiter DEX Aggregator)"""
        # 1. 代幣名稱解析
        MINT_MAP = {
            "SOL": "So11111111111111111111111111111111111111112",
            "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        }
        
        from_mint = MINT_MAP.get(from_token.upper(), from_token)
        to_mint = MINT_MAP.get(to_token.upper(), to_token)
        
        # 2. 檢查 GAS (SOL)
        sol_bal = self.wallet.get_sol_balance()
        if sol_bal < 0.001:
            return f"❌ 交易失敗：您的 Solana 錢包中 SOL 餘額不足 (`{sol_bal:.6f} SOL`)，無法支付 Gas 費用。請先轉入至少 0.01 SOL。"

        # 3. 獲取報價 (Quote)
        # 需要處理 amount 的小數位。USDT/USDC 是 6 位，SOL 是 9 位。
        # 這裡簡單假設：如果是 SOL -> 10^9, 否則 10^6 (USDT/USDC)
        decimals = 6
        if from_token.upper() == "SOL" or from_mint == MINT_MAP["SOL"]:
            decimals = 9
            
        amount_atoms = int(amount * (10 ** decimals))
        
        logger.info(f"正在獲取 Jupiter 報價: {amount} {from_token} -> {to_token}")
        quote = await self.wallet.get_jupiter_quote(from_mint, to_mint, amount_atoms)
        
        if not quote:
            return f"❌ 無法獲取從 {from_token} 到 {to_token} 的交易報價，請稍後再試或檢查代幣位址。"

        expected_out = float(quote.get("outAmount", 0)) / (10 ** (9 if to_token.upper() == "SOL" else 6))
        
        # 4. 執行 Swap
        logger.info(f"正在執行 Jupiter Swap... 預計獲得: {expected_out} {to_token}")
        success, tx_hash = await self.wallet.execute_jupiter_swap(quote)
        
        if success:
            return f"✅ **跨鏈 Swap 成功！**\n- **賣出**: `{amount} {from_token}`\n- **買入 (預計)**: `{expected_out:.6f} {to_token}`\n- **交易哈希**: `{tx_hash}`\n- [在 Solscan 查看](https://solscan.io/tx/{tx_hash})"
        else:
            return f"❌ **交易失敗**：{tx_hash}"

    async def check_health(self) -> str:
        """評估目前資金健康狀況"""
        # 注意：AgentWallet 底層的 check_health 包含了對 ETH 與 USDC 的檢查
        # 如果未來加上 SOL 維持費用的檢查，可在底層擴充
        status = self.wallet.check_health()
        if status == "HEALTHY":
            return "✅ 狀態：`HEALTHY`。我的資金充足，可以確保自動化任務的 Gas 費用。"
        else:
            return "🚨 狀態：`CRITICAL_LOW_FUNDS`。資金即將見底，請轉入一些 ETH/SOL 至我的錢包以維持運作！"
