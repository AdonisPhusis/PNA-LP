"""
Dash RPC Client for pna SDK.

Supports Dash Core RPC for Testnet/Mainnet.
Dash is a Bitcoin/PIVX fork with 100% compatible RPC.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class DASHConfig:
    """Dash node configuration."""
    network: str = "testnet"
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 19998           # Default testnet RPC port
    rpc_user: str = ""
    rpc_password: str = ""
    wallet_name: str = ""
    cli_path: Optional[Path] = None
    datadir: str = ""


class DASHClient:
    """Dash RPC client via dash-cli."""

    def __init__(self, config: DASHConfig):
        self.config = config
        self.cli_path = config.cli_path or self._find_cli()

    def _find_cli(self) -> Optional[Path]:
        paths = [
            Path.home() / "dash" / "bin" / "dash-cli",
            Path("/usr/local/bin/dash-cli"),
            Path("/usr/bin/dash-cli"),
        ]
        for p in paths:
            if p.exists():
                return p
        return None

    def _get_network_flag(self) -> str:
        return "-testnet" if self.config.network == "testnet" else ""

    def _build_cmd(self, method: str, *args) -> List[str]:
        if not self.cli_path:
            raise RuntimeError("dash-cli not found")
        cmd = [str(self.cli_path)]
        net_flag = self._get_network_flag()
        if net_flag:
            cmd.append(net_flag)
        if self.config.datadir:
            cmd.append(f"-datadir={self.config.datadir}")
        if self.config.rpc_user:
            cmd.append("-rpcuser=" + self.config.rpc_user)
        if self.config.rpc_password:
            cmd.append("-rpcpassword=" + self.config.rpc_password)
        cmd.append(method)
        cmd.extend(str(a).lower() if isinstance(a, bool) else str(a) for a in args)
        return cmd

    def _call(self, method: str, *args, timeout: int = 30) -> Any:
        cmd = self._build_cmd(method, *args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                error = result.stderr.strip()
                log.error(f"DASH RPC error: {method} -> {error}")
                raise RuntimeError(f"DASH RPC failed: {error}")
            output = result.stdout.strip()
            if not output:
                return None
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"DASH RPC timeout: {method}")

    # ── Wallet ────────────────────────────────────────────────────────────────

    def _ensure_wallet(self) -> None:
        """Create default wallet if none loaded (Dash v23+ requires explicit createwallet)."""
        try:
            wallets = self._call("listwallets")
            if isinstance(wallets, list) and len(wallets) > 0:
                return  # Wallet already loaded
        except RuntimeError:
            pass
        # Try to load or create
        try:
            self._call("createwallet", "lp_dash_wallet")
            log.info("DASH: Created wallet 'lp_dash_wallet'")
        except RuntimeError as e:
            if "already exists" in str(e).lower():
                try:
                    self._call("loadwallet", "lp_dash_wallet")
                    log.info("DASH: Loaded existing wallet 'lp_dash_wallet'")
                except RuntimeError:
                    pass
            else:
                log.warning(f"DASH: Could not create wallet: {e}")

    def get_new_address(self, label: str = "") -> str:
        self._ensure_wallet()
        if label:
            return self._call("getnewaddress", label)
        return self._call("getnewaddress")

    def get_addresses_by_label(self, label: str) -> List[str]:
        try:
            result = self._call("getaddressesbylabel", label)
            return list(result.keys()) if isinstance(result, dict) else []
        except RuntimeError:
            return []

    # ── Balance ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        try:
            result = self._call("getbalance")
            return float(result) if result is not None else 0
        except (RuntimeError, ValueError):
            return 0

    def list_unspent(self, addresses: List[str] = None,
                     min_conf: int = 0, max_conf: int = 9999999) -> List[Dict]:
        if addresses:
            return self._call("listunspent", min_conf, max_conf, json.dumps(addresses))
        return self._call("listunspent", min_conf, max_conf)

    # ── Transactions ──────────────────────────────────────────────────────────

    def get_transaction(self, txid: str) -> Optional[Dict]:
        try:
            return self._call("gettransaction", txid)
        except RuntimeError:
            return None

    def send_to_address(self, address: str, amount: float, comment: str = "") -> str:
        if comment:
            return self._call("sendtoaddress", address, str(amount), comment)
        return self._call("sendtoaddress", address, str(amount))

    # ── Blockchain ────────────────────────────────────────────────────────────

    def get_block_count(self) -> int:
        return self._call("getblockcount")

    def get_blockchain_info(self) -> Dict:
        return self._call("getblockchaininfo")

    def validate_address(self, address: str) -> Dict:
        return self._call("validateaddress", address)
