"""
PIVX RPC Client for pna SDK.

Supports PIVX Core RPC for Testnet/Mainnet.
PIVX is a Bitcoin/Dash fork with 100% compatible RPC.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class PIVXConfig:
    """PIVX node configuration."""
    network: str = "testnet"        # testnet, mainnet
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 51476           # PIVX testnet RPC (51475 conflicts with bathrond)
    rpc_user: str = ""
    rpc_password: str = ""
    wallet_name: str = ""           # Empty = use default wallet
    cli_path: Optional[Path] = None # Path to pivx-cli
    datadir: str = ""               # -datadir for pivx-cli


class PIVXClient:
    """
    PIVX RPC client.

    Uses pivx-cli for simplicity and reliability.
    RPC is 100% Bitcoin-compatible.
    """

    def __init__(self, config: PIVXConfig):
        self.config = config
        self.cli_path = config.cli_path or self._find_cli()

    def _find_cli(self) -> Optional[Path]:
        """Find pivx-cli binary."""
        paths = [
            Path.home() / "pivx" / "bin" / "pivx-cli",
            Path("/usr/local/bin/pivx-cli"),
            Path("/usr/bin/pivx-cli"),
        ]
        for p in paths:
            if p.exists():
                return p
        return None

    def _get_network_flag(self) -> str:
        """Get network flag for CLI."""
        flags = {
            "testnet": "-testnet",
            "mainnet": "",
        }
        return flags.get(self.config.network, "-testnet")

    def _build_cmd(self, method: str, *args) -> List[str]:
        """Build CLI command."""
        if not self.cli_path:
            raise RuntimeError("pivx-cli not found")

        cmd = [str(self.cli_path)]

        # Network flag
        net_flag = self._get_network_flag()
        if net_flag:
            cmd.append(net_flag)

        # Datadir if provided
        if self.config.datadir:
            cmd.append(f"-datadir={self.config.datadir}")

        # RPC credentials if provided
        if self.config.rpc_user:
            cmd.extend(["-rpcuser=" + self.config.rpc_user])
        if self.config.rpc_password:
            cmd.extend(["-rpcpassword=" + self.config.rpc_password])

        # Method and args
        cmd.append(method)
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
                log.error(f"PIVX RPC error: {method} -> {error}")
                raise RuntimeError(f"PIVX RPC failed: {error}")

            output = result.stdout.strip()
            if not output:
                return None

            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"PIVX RPC timeout: {method}")

    # =========================================================================
    # Wallet Operations
    # =========================================================================

    def get_new_address(self, label: str = "") -> str:
        """Generate new address. PIVX testnet uses legacy (y...) by default."""
        if label:
            return self._call("getnewaddress", label)
        return self._call("getnewaddress")

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
        try:
            result = self._call("getbalance")
            return float(result) if result is not None else 0
        except (RuntimeError, ValueError):
            return 0

    def list_unspent(self, addresses: List[str] = None,
                     min_conf: int = 0, max_conf: int = 9999999) -> List[Dict]:
        """List unspent outputs."""
        if addresses:
            return self._call("listunspent", min_conf, max_conf, json.dumps(addresses))
        return self._call("listunspent", min_conf, max_conf)

    def get_balance_for_addresses(self, addresses: List[str]) -> tuple:
        """Get balance for specific addresses. Returns (confirmed, pending)."""
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

    def send_to_address(self, address: str, amount: float, comment: str = "") -> str:
        """Send PIVX to an address."""
        if comment:
            return self._call("sendtoaddress", address, str(amount), comment)
        return self._call("sendtoaddress", address, str(amount))

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

    # =========================================================================
    # Address Operations
    # =========================================================================

    def validate_address(self, address: str) -> Dict:
        """Validate and get info about an address."""
        return self._call("validateaddress", address)
