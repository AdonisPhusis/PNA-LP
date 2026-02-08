"""
Bitcoin RPC Client for pna SDK.

Supports Bitcoin Core RPC for Signet/Testnet/Mainnet.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class BTCConfig:
    """Bitcoin node configuration."""
    network: str = "signet"         # signet, testnet, mainnet
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 38332           # Default signet port
    rpc_user: str = ""
    rpc_password: str = ""
    wallet_name: str = ""              # Empty = use default loaded wallet
    cli_path: Optional[Path] = None  # Path to bitcoin-cli
    datadir: str = ""               # -datadir for bitcoin-cli (e.g. ~/.bitcoin-signet)


class BTCClient:
    """
    Bitcoin RPC client.

    Uses bitcoin-cli for simplicity and reliability.
    Can be extended to use JSON-RPC directly.
    """

    def __init__(self, config: BTCConfig):
        self.config = config
        self.cli_path = config.cli_path or self._find_cli()

    def _find_cli(self) -> Optional[Path]:
        """Find bitcoin-cli binary."""
        paths = [
            Path.home() / "bitcoin" / "bin" / "bitcoin-cli",
            Path("/usr/local/bin/bitcoin-cli"),
            Path("/usr/bin/bitcoin-cli"),
        ]
        for p in paths:
            if p.exists():
                return p
        return None

    def _get_network_flag(self) -> str:
        """Get network flag for CLI."""
        flags = {
            "signet": "-signet",
            "testnet": "-testnet",
            "mainnet": "",
        }
        return flags.get(self.config.network, "-signet")

    def _build_cmd(self, method: str, *args) -> List[str]:
        """Build CLI command."""
        if not self.cli_path:
            raise RuntimeError("bitcoin-cli not found")

        cmd = [str(self.cli_path)]

        # Network flag
        net_flag = self._get_network_flag()
        if net_flag:
            cmd.append(net_flag)

        # Datadir if provided (needed for cookie auth)
        if self.config.datadir:
            cmd.append(f"-datadir={self.config.datadir}")

        # Wallet name (required for multi-wallet setups)
        if self.config.wallet_name:
            cmd.append(f"-rpcwallet={self.config.wallet_name}")

        # RPC credentials if provided
        if self.config.rpc_user:
            cmd.extend(["-rpcuser=" + self.config.rpc_user])
        if self.config.rpc_password:
            cmd.extend(["-rpcpassword=" + self.config.rpc_password])

        # Method and args
        cmd.append(method)
        # Convert booleans to lowercase JSON format (true/false not True/False)
        cmd.extend(str(a).lower() if isinstance(a, bool) else str(a) for a in args)

        return cmd

    def _call(self, method: str, *args, timeout: int = 30) -> Any:
        """Execute RPC call via CLI."""
        cmd = self._build_cmd(method, *args)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode != 0:
                error = result.stderr.strip()
                log.error(f"BTC RPC error: {method} -> {error}")
                raise RuntimeError(f"BTC RPC failed: {error}")

            output = result.stdout.strip()
            if not output:
                return None

            # Try to parse as JSON
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"BTC RPC timeout: {method}")

    # =========================================================================
    # Wallet Operations
    # =========================================================================

    def create_wallet(self, name: str = None) -> bool:
        """Create wallet if it doesn't exist."""
        name = name or self.config.wallet_name
        try:
            self._call("createwallet", name)
            return True
        except RuntimeError as e:
            if "already exists" in str(e).lower():
                return True
            log.warning(f"Create wallet failed: {e}")
            return False

    def load_wallet(self, name: str = None) -> bool:
        """Load wallet."""
        name = name or self.config.wallet_name
        try:
            self._call("loadwallet", name)
            return True
        except RuntimeError as e:
            if "already loaded" in str(e).lower():
                return True
            log.warning(f"Load wallet failed: {e}")
            return False

    def get_new_address(self, label: str = "", address_type: str = "bech32") -> str:
        """Generate new address."""
        return self._call("getnewaddress", label, address_type)

    def get_addresses_by_label(self, label: str) -> List[str]:
        """Get all addresses for a label."""
        try:
            result = self._call("getaddressesbylabel", label)
            if isinstance(result, dict):
                return list(result.keys())
            return []
        except RuntimeError:
            return []

    # =========================================================================
    # Balance & UTXOs
    # =========================================================================

    def get_balance(self) -> float:
        """Get wallet balance."""
        info = self._call("getwalletinfo")
        return info.get("balance", 0) if info else 0

    def list_unspent(self, addresses: List[str] = None,
                     min_conf: int = 0, max_conf: int = 9999999) -> List[Dict]:
        """List unspent outputs."""
        if addresses:
            return self._call("listunspent", min_conf, max_conf, json.dumps(addresses))
        return self._call("listunspent", min_conf, max_conf)

    def get_balance_for_addresses(self, addresses: List[str]) -> tuple[float, float]:
        """
        Get balance for specific addresses.

        Returns:
            (confirmed_balance, pending_balance)
        """
        utxos = self.list_unspent(addresses)
        if not utxos:
            return 0.0, 0.0

        confirmed = sum(u["amount"] for u in utxos if u.get("confirmations", 0) > 0)
        pending = sum(u["amount"] for u in utxos if u.get("confirmations", 0) == 0)
        return confirmed, pending

    # =========================================================================
    # Transaction Operations
    # =========================================================================

    def get_transaction(self, txid: str) -> Optional[Dict]:
        """Get transaction details."""
        try:
            return self._call("gettransaction", txid)
        except RuntimeError:
            return None

    def get_raw_transaction(self, txid: str, verbose: bool = True) -> Optional[Dict]:
        """Get raw transaction with details."""
        try:
            return self._call("getrawtransaction", txid, verbose)
        except RuntimeError:
            return None

    def send_raw_transaction(self, hex_tx: str) -> str:
        """Broadcast raw transaction."""
        return self._call("sendrawtransaction", hex_tx)

    def create_raw_transaction(self, inputs: List[Dict], outputs: Dict) -> str:
        """Create unsigned raw transaction."""
        return self._call("createrawtransaction",
                         json.dumps(inputs), json.dumps(outputs))

    def sign_raw_transaction(self, hex_tx: str) -> Dict:
        """Sign raw transaction."""
        return self._call("signrawtransactionwithwallet", hex_tx)

    def send_to_address(self, address: str, amount: float, comment: str = "") -> str:
        """
        Send BTC to an address.

        Args:
            address: Destination BTC address
            amount: Amount in BTC (e.g., 0.001)
            comment: Optional comment for the transaction

        Returns:
            Transaction ID (txid)
        """
        if comment:
            return self._call("sendtoaddress", address, str(amount), comment)
        return self._call("sendtoaddress", address, str(amount))

    def decode_raw_transaction(self, hex_tx: str) -> Dict:
        """Decode raw transaction."""
        return self._call("decoderawtransaction", hex_tx)

    # =========================================================================
    # Blockchain Info
    # =========================================================================

    def get_block_count(self) -> int:
        """Get current block height."""
        return self._call("getblockcount")

    def get_block_hash(self, height: int) -> str:
        """Get block hash at height."""
        return self._call("getblockhash", height)

    def get_blockchain_info(self) -> Dict:
        """Get blockchain info."""
        return self._call("getblockchaininfo")

    def estimate_smart_fee(self, conf_target: int = 6) -> float:
        """Estimate fee rate (BTC/kB)."""
        result = self._call("estimatesmartfee", conf_target)
        return result.get("feerate", 0.00001) if result else 0.00001

    # =========================================================================
    # Address Operations
    # =========================================================================

    def validate_address(self, address: str) -> Dict:
        """Validate and get info about an address."""
        return self._call("validateaddress", address)

    def get_address_info(self, address: str) -> Dict:
        """Get detailed address info."""
        return self._call("getaddressinfo", address)

    def decode_script(self, hex_script: str) -> Dict:
        """Decode a script."""
        return self._call("decodescript", hex_script)

    # =========================================================================
    # PSBT Operations (for advanced HTLC handling)
    # =========================================================================

    def create_psbt(self, inputs: List[Dict], outputs: Dict) -> str:
        """Create a PSBT."""
        return self._call("createpsbt", json.dumps(inputs), json.dumps(outputs))

    def decode_psbt(self, psbt: str) -> Dict:
        """Decode a PSBT."""
        return self._call("decodepsbt", psbt)

    def finalize_psbt(self, psbt: str) -> Dict:
        """Finalize a PSBT."""
        return self._call("finalizepsbt", psbt)
