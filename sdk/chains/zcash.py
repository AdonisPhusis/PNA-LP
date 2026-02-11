"""
Zcash RPC Client for pna SDK.

Supports Zcash Core RPC for Testnet/Mainnet.
Zcash is a Bitcoin fork with compatible transparent RPC.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class ZECConfig:
    """Zcash node configuration."""
    network: str = "testnet"
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 18232           # Default testnet RPC port
    rpc_user: str = ""
    rpc_password: str = ""
    cli_path: Optional[Path] = None
    datadir: str = ""


class ZECClient:
    """Zcash RPC client via zcash-cli."""

    def __init__(self, config: ZECConfig):
        self.config = config
        self.cli_path = config.cli_path or self._find_cli()

    def _find_cli(self) -> Optional[Path]:
        paths = [
            Path.home() / "zcash" / "bin" / "zcash-cli",
            Path("/usr/local/bin/zcash-cli"),
            Path("/usr/bin/zcash-cli"),
        ]
        for p in paths:
            if p.exists():
                return p
        return None

    def _get_network_flag(self) -> str:
        return "-testnet" if self.config.network == "testnet" else ""

    def _build_cmd(self, method: str, *args) -> List[str]:
        if not self.cli_path:
            raise RuntimeError("zcash-cli not found")
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
                log.error(f"ZEC RPC error: {method} -> {error}")
                raise RuntimeError(f"ZEC RPC failed: {error}")
            output = result.stdout.strip()
            if not output:
                return None
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ZEC RPC timeout: {method}")

    # ── Wallet ────────────────────────────────────────────────────────────────

    def get_new_address(self) -> str:
        """Generate new transparent address (t-addr)."""
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
