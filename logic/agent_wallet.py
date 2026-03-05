import os
import logging
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.exceptions import Web3Exception
from eth_account import Account
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client as SolanaClient
from solana.exceptions import SolanaRpcException
from solana.rpc.types import TokenAccountOpts
from solders.transaction import VersionedTransaction
import httpx
import base64
from dotenv import load_dotenv, set_key

# 設定 logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AgentWallet")

# --- Solana Mint Addresses ---
SOL_MINT = "So11111111111111111111111111111111111111112"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

class AgentWallet:
    def __init__(self, rpc_url="https://mainnet.base.org", env_path=".env"):
        self.env_path = env_path
        load_dotenv(dotenv_path=self.env_path)
        
        # 讀取 RPC URL（支援從環境變數覆蓋）
        self.rpc_url = os.getenv("RPC_URL", rpc_url)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        
        # Base network 兼容以太坊，但為了保險起見加入 POA middleware
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        
        if not self.w3.is_connected():
            logger.error(f"無法連接到 RPC 節點: {self.rpc_url}")
            
        self.__private_key = None
        self.address = None
        
        self._initialize_wallet()
        
        # --- Solana Initialization ---
        self.solana_rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.solana_client = SolanaClient(self.solana_rpc_url)
        self.__sol_keypair = None
        self.sol_address = None
        
        self._initialize_solana_wallet()

    def _initialize_solana_wallet(self):
        """初始化 Solana 錢包：如果有私鑰就讀取，沒有就產生一組新的並安全儲存"""
        sol_priv_key_str = os.getenv("AGENT_SOLANA_PRIVATE_KEY")
        if sol_priv_key_str:
            logger.info("在 .env 中找到現有的 Solana 錢包")
            try:
                # 假設儲存的是 base58 字串或是 bytecode list
                # 這裡最簡單的方式是存取與讀取 base58 或 comma-separated string
                # 為了通用性與相容 Phantom 等錢包，常存儲為 base58
                import base58
                key_bytes = base58.b58decode(sol_priv_key_str)
                self.__sol_keypair = Keypair.from_bytes(key_bytes)
                self.sol_address = str(self.__sol_keypair.pubkey())
            except Exception as e:
                logger.error(f"解析 Solana 私鑰失敗，將重新生成: {e}")
                self.__generate_and_save_solana_key()
        else:
            self.__generate_and_save_solana_key()
            
        logger.info(f"Agent Solana 錢包地址 (Public Key): {self.sol_address}")

    def __generate_and_save_solana_key(self):
        import base58
        logger.info("未找到 Solana 錢包。正在生成全新的 Solana 錢包...")
        self.__sol_keypair = Keypair()
        self.sol_address = str(self.__sol_keypair.pubkey())
        
        # 安全地將私鑰寫入 .env (Base58 格式)
        priv_key_b58 = base58.b58encode(bytes(self.__sol_keypair)).decode('utf-8')
        
        if not os.path.exists(self.env_path):
            open(self.env_path, 'a').close()
        set_key(self.env_path, "AGENT_SOLANA_PRIVATE_KEY", priv_key_b58)
        logger.info("新 Solana 錢包已創建並安全儲存至 .env 檔案中")

    def _initialize_wallet(self):
        """初始化錢包：如果有私鑰就讀取，沒有就產生一組新的並安全儲存"""
        priv_key_hex = os.getenv("AGENT_PRIVATE_KEY")
        if priv_key_hex:
            logger.info("在 .env 中找到現有的錢包")
            account = Account.from_key(priv_key_hex)
            self.__private_key = priv_key_hex
            self.address = account.address
        else:
            logger.info("未找到錢包。正在生成全新的錢包...")
            account = Account.create()
            self.__private_key = account.key.hex()
            self.address = account.address
            
            # 安全地將私鑰寫入 .env
            if not os.path.exists(self.env_path):
                open(self.env_path, 'a').close()
            set_key(self.env_path, "AGENT_PRIVATE_KEY", self.__private_key)
            logger.info("新錢包已創建並安全儲存至 .env 檔案中")
            
        # 安全紅線：對外只允許顯示錢包地址（Public Key），絕對禁止印出私鑰
        logger.info(f"Agent 錢包地址 (Public Key): {self.address}")

    def get_gas_balance(self):
        """查詢用來支付 Gas 的原生代幣（ETH）餘額"""
        try:
            balance_wei = self.w3.eth.get_balance(self.address)
            balance_eth = self.w3.from_wei(balance_wei, 'ether')
            return balance_eth
        except Web3Exception as e:
            logger.error(f"獲取 Gas 餘額時發生錯誤: {e}")
            return 0

    def get_sol_balance(self):
        """查詢 Solana (SOL) 餘額"""
        try:
            pubkey = Pubkey.from_string(self.sol_address)
            response = self.solana_client.get_balance(pubkey)
            # Solana 的最小單位是 lamport，1 SOL = 10^9 lamports
            return response.value / 1_000_000_000
        except Exception as e:
            logger.error(f"獲取 SOL 餘額時發生錯誤: {e}")
            return 0

    def get_spl_token_balance(self, mint_address):
        """查詢特定的 SPL 代幣餘額"""
        try:
            mint_pubkey = Pubkey.from_string(mint_address)
            owner_pubkey = Pubkey.from_string(self.sol_address)
            
            # 獲取該 Mint 的所有 Token Accounts
            opts = TokenAccountOpts(mint=mint_pubkey)
            response = self.solana_client.get_token_accounts_by_owner(owner_pubkey, opts)
            
            total_balance = 0
            if response.value:
                for account in response.value:
                    # 獲取該帳戶的詳細餘額資訊
                    bal_resp = self.solana_client.get_token_account_balance(account.pubkey)
                    if bal_resp.value:
                        total_balance += float(bal_resp.value.ui_amount)
            return total_balance
        except Exception as e:
            logger.error(f"獲取 SPL 代幣 ({mint_address}) 餘額時發生錯誤: {e}")
            return 0

    def get_token_balance(self, token_address):
        """查詢特定的 ERC-20 代幣餘額"""
        # 查詢餘額所需的最小 ERC20 ABI
        abi = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function",
            },
            {
                "constant": True,
                "inputs": [],
                "name": "decimals",
                "outputs": [{"name": "", "type": "uint8"}],
                "type": "function",
            }
        ]
        try:
            # 轉換為 Checksum Address
            checksum_address = self.w3.to_checksum_address(token_address)
            contract = self.w3.eth.contract(address=checksum_address, abi=abi)
            balance = contract.functions.balanceOf(self.address).call()
            decimals = contract.functions.decimals().call()
            # 將最小單位轉換為正常數量
            return balance / (10 ** decimals)
        except Exception as e:
            logger.error(f"獲取代幣 ({token_address}) 餘額時發生錯誤: {e}")
            return 0

    def transfer_token(self, to_address, amount, token_address=None):
        """基礎轉帳函數：可轉帳 ETH 或 ERC20 代幣"""
        try:
            to_address = self.w3.to_checksum_address(to_address)
            nonce = self.w3.eth.get_transaction_count(self.address)
            
            if token_address is None:
                # 轉帳原生 ETH
                # 轉換數值為 Wei
                value_wei = self.w3.to_wei(amount, 'ether')
                tx = {
                    'nonce': nonce,
                    'to': to_address,
                    'value': value_wei,
                    'gas': 21000,
                    'gasPrice': self.w3.eth.gas_price,
                    'chainId': self.w3.eth.chain_id
                }
            else:
                # 轉帳 ERC20
                token_address = self.w3.to_checksum_address(token_address)
                abi = [
                    {
                        "constant": False,
                        "inputs": [
                            {"name": "_to", "type": "address"},
                            {"name": "_value", "type": "uint256"}
                        ],
                        "name": "transfer",
                        "outputs": [{"name": "", "type": "bool"}],
                        "type": "function",
                    },
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "decimals",
                        "outputs": [{"name": "", "type": "uint8"}],
                        "type": "function",
                    }
                ]
                contract = self.w3.eth.contract(address=token_address, abi=abi)
                decimals = contract.functions.decimals().call()
                amount_wei = int(amount * (10 ** decimals))
                
                # 估算轉帳所需的 Gas
                gas_estimate = contract.functions.transfer(to_address, amount_wei).estimate_gas({'from': self.address})
                
                tx = contract.functions.transfer(to_address, amount_wei).build_transaction({
                    'chainId': self.w3.eth.chain_id,
                    'gas': gas_estimate,
                    'gasPrice': self.w3.eth.gas_price,
                    'nonce': nonce,
                })

            # 使用私鑰對交易進行簽名
            signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.__private_key)
            
            # 發送交易並獲取交易哈希
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            logger.info(f"交易已發送! 交易哈希 (Hash): {self.w3.to_hex(tx_hash)}")
            
            # 等待交易被打包並取得收據
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt.status == 1:
                logger.info("交易成功打包！")
                return True, receipt.transactionHash.hex()
            else:
                logger.error("交易失敗！(Reverted)")
                return False, receipt.transactionHash.hex()
                
        except Exception as e:
            logger.error(f"轉帳過程中發生錯誤: {e}")
            return False, str(e)

    def check_health(self):
        """生存監控：評估 Agent 所需資金是否低於安全線"""
        # Base 網絡的 USDC 地址
        USDC_ADDRESS_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        
        # 獲取餘額
        eth_balance = self.get_gas_balance()
        usdc_balance = self.get_token_balance(USDC_ADDRESS_BASE)
        
        # Logger 僅顯示必要的財務摘要
        logger.info(f"系統狀態 -> ETH: {eth_balance:.6f}, USDC: {usdc_balance:.2f}")
        
        # 定義安全線：如果 ETH 不足 0.001 或 USDC 不足 2 則報警
        if eth_balance < 0.001 or usdc_balance < 2:
            status = "CRITICAL_LOW_FUNDS"
            logger.warning(f"生存警告: {status}。需要提供資金以利後續自動化作業！")
        else:
            status = "HEALTHY"
            logger.info(f"生存狀態: {status}。資金充足。")
            
        return status

    # --- Solana Jupiter Swap Methods ---

    async def get_jupiter_quote(self, input_mint, output_mint, amount_atoms, slippage_bps=50):
        """從 Jupiter API 獲取交易報價 (Quote)"""
        url = "https://lite-api.jup.ag/swap/v1/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_atoms)),
            "slippageBps": slippage_bps
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Jupiter Quote 失敗: {response.text}")
                    return None
        except Exception as e:
            logger.error(f"獲取 Jupiter 報價時出錯: {e}")
            return None

    async def execute_jupiter_swap(self, quote_response):
        """執行 Jupiter Swap：獲取序列化交易、簽名並發送"""
        if not quote_response:
            return False, "無效的報價數據"

        swap_url = "https://lite-api.jup.ag/swap/v1/swap"
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": self.sol_address,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto"
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(swap_url, json=payload)
                if response.status_code != 200:
                    return False, f"獲取 Swap 交易失敗: {response.text}"
                
                swap_data = response.json()
                swap_transaction_base64 = swap_data.get("swapTransaction")
                
                # 1. 反序列化交易
                raw_tx = base64.b64decode(swap_transaction_base64)
                versioned_tx = VersionedTransaction.from_bytes(raw_tx)
                
                # 2. 簽名交易 (solders VersionedTransaction 需要傳入所有需要的簽名者)
                # 這裡假設只有 Agent 本身需要簽名
                signed_tx = VersionedTransaction(versioned_tx.message, [self.__sol_keypair])
                
                # 3. 發送交易
                # 注意：solders 序列化後可以直接發送
                res = self.solana_client.send_raw_transaction(bytes(signed_tx))
                
                if res.value:
                    tx_hash = str(res.value)
                    logger.info(f"Jupiter Swap 已發送! Hash: {tx_hash}")
                    return True, tx_hash
                else:
                    return False, "發送交易失敗 (無回傳 Hash)"
                    
        except Exception as e:
            logger.error(f"執行 Jupiter Swap 時出錯: {e}")
            return False, str(e)

if __name__ == "__main__":
    # 簡單的測試執行代碼，實際佈署時會由其他模塊引用 AgentWallet 類別
    wallet = AgentWallet()
    wallet.check_health()
