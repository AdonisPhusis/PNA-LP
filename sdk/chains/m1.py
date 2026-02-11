"""
BATHRON (M1) RPC Client for pna SDK.

Supports BATHRON node RPC including HTLC operations.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class M1Config:
    """BATHRON node configuration."""
    network: str = "testnet"        # testnet, mainnet
    rpc_host: Optional[str] = None  # None = use default from config file
    rpc_port: Optional[int] = None  # None = use default (27171 testnet)
    rpc_user: Optional[str] = None
    rpc_password: Optional[str] = None
    cli_path: Optional[Path] = None


class M1Client:
    """
    BATHRON (M1) RPC client.

    Provides access to:
    - Standard wallet operations
    - M1 receipt management (lock/unlock)
    - HTLC operations (create, claim, refund)
    """

    def __init__(self, config: M1Config):
        self.config = config
        self.cli_path = config.cli_path or self._find_cli()

    def _find_cli(self) -> Optional[Path]:
        """Find bathron-cli binary."""
        paths = [
            Path.home() / "bathron" / "bin" / "bathron-cli",
            Path.home() / "BATHRON" / "src" / "bathron-cli",
            Path("/usr/local/bin/bathron-cli"),
        ]
        for p in paths:
            if p.exists():
                return p
        return None

    def _get_network_flag(self) -> str:
        """Get network flag for CLI."""
        return "-testnet" if self.config.network == "testnet" else ""

    def _build_cmd(self, method: str, *args) -> List[str]:
        """Build CLI command."""
        if not self.cli_path:
            raise RuntimeError("bathron-cli not found")

        cmd = [str(self.cli_path)]

        # Network flag
        net_flag = self._get_network_flag()
        if net_flag:
            cmd.append(net_flag)

        # RPC connection - only add if explicitly configured
        # Otherwise use defaults from ~/.bathron/bathron.conf
        if self.config.rpc_host:
            cmd.append(f"-rpcconnect={self.config.rpc_host}")
        if self.config.rpc_port:
            cmd.append(f"-rpcport={self.config.rpc_port}")
        if self.config.rpc_user:
            cmd.append(f"-rpcuser={self.config.rpc_user}")
        if self.config.rpc_password:
            cmd.append(f"-rpcpassword={self.config.rpc_password}")

        # Method and args
        cmd.append(method)
        # Convert booleans to lowercase strings (CLI expects "true"/"false", not "True"/"False")
        cmd.extend(str(a).lower() if isinstance(a, bool) else str(a) for a in args)

        return cmd

    def _call(self, method: str, *args, timeout: int = 30) -> Any:
        """Execute RPC call via CLI."""
        cmd = self._build_cmd(method, *args)

        # Debug log the command
        log.info(f"M1 RPC cmd: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode != 0:
                error = result.stderr.strip()
                log.error(f"M1 RPC error: {method} -> {error}")
                log.error(f"M1 RPC cmd was: {' '.join(cmd)}")
                raise RuntimeError(f"M1 RPC failed: {error}")

            output = result.stdout.strip()
            if not output:
                return None

            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"M1 RPC timeout: {method}")

    # =========================================================================
    # Wallet Operations
    # =========================================================================

    def get_new_address(self, label: str = "") -> str:
        """Generate new address."""
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

    def validate_address(self, address: str) -> Dict:
        """Validate address."""
        return self._call("validateaddress", address)

    # =========================================================================
    # Balance & UTXOs
    # =========================================================================

    def get_balance(self) -> float:
        """Get wallet M0 balance."""
        return self._call("getbalance")

    def list_unspent(self, addresses: List[str] = None,
                     min_conf: int = 0, max_conf: int = 9999999) -> List[Dict]:
        """List unspent outputs."""
        if addresses:
            return self._call("listunspent", min_conf, max_conf, json.dumps(addresses))
        return self._call("listunspent", min_conf, max_conf)

    def get_balance_for_addresses(self, addresses: List[str]) -> tuple[float, float]:
        """Get balance for specific addresses."""
        utxos = self.list_unspent(addresses)
        if not utxos:
            return 0.0, 0.0

        confirmed = sum(u["amount"] for u in utxos if u.get("confirmations", 0) > 0)
        pending = sum(u["amount"] for u in utxos if u.get("confirmations", 0) == 0)
        return confirmed, pending

    # =========================================================================
    # M1 Receipt Operations
    # =========================================================================

    def get_wallet_state(self, verbose: bool = True) -> Dict:
        """Get wallet state including M1 receipts."""
        return self._call("getwalletstate", verbose)

    def get_state(self) -> Dict:
        """Get global settlement state (M0/M1 totals)."""
        return self._call("getstate")

    def lock(self, amount: int) -> Dict:
        """Lock M0 -> M1 receipt."""
        return self._call("lock", amount)

    def unlock(self, amount: int) -> Dict:
        """Unlock M1 receipt -> M0."""
        return self._call("unlock", amount)

    def transfer_m1(self, outpoint: str, to_address: str) -> Dict:
        """Transfer M1 receipt to another address."""
        return self._call("transfer_m1", outpoint, to_address)

    def list_m1_receipts(self) -> List[Dict]:
        """List all M1 receipts in wallet."""
        state = self.get_wallet_state(True)
        # Receipts are nested under state["m1"]["receipts"]
        if not state:
            return []
        m1_state = state.get("m1", {})
        return m1_state.get("receipts", []) if m1_state else []

    # =========================================================================
    # HTLC Operations
    # =========================================================================

    def htlc_generate(self) -> Dict:
        """
        Generate a new secret and hashlock for HTLC.

        Returns:
            {"secret": "hex...", "hashlock": "hex..."}
        """
        return self._call("htlc_generate")

    def htlc_create_m1(self, receipt_outpoint: str, hashlock: str,
                       claim_address: str, expiry_blocks: int = 288) -> Dict:
        """
        Create HTLC from M1 receipt.

        Args:
            receipt_outpoint: M1 receipt to lock (txid:vout)
            hashlock: SHA256 hash (64 hex chars)
            claim_address: Address that can claim with preimage
            expiry_blocks: Blocks until refund allowed (default ~2 days)
                          NOTE: Due to CLI integer parsing bug, we always
                          use the RPC default (288) for now.

        Returns:
            {"txid": "...", "htlc_address": "...", ...}
        """
        # NOTE: CLI has a bug where numeric 4th arg isn't parsed as integer.
        # Always use the RPC default expiry (288 blocks = ~4.8 hours).
        # TODO: Fix CLI parsing and re-enable custom expiry.
        return self._call("htlc_create_m1", receipt_outpoint, hashlock,
                         claim_address)

    def htlc_claim(self, htlc_outpoint: str, preimage: str) -> Dict:
        """
        Claim HTLC with preimage.

        Args:
            htlc_outpoint: HTLC to claim (txid:vout)
            preimage: 32-byte preimage (64 hex chars)

        Returns:
            {"txid": "...", "receipt_outpoint": "..."}
        """
        return self._call("htlc_claim", htlc_outpoint, preimage)

    def htlc_refund(self, htlc_outpoint: str) -> Dict:
        """
        Refund expired HTLC.

        Args:
            htlc_outpoint: Expired HTLC to refund (txid:vout)

        Returns:
            {"txid": "...", "receipt_outpoint": "..."}
        """
        return self._call("htlc_refund", htlc_outpoint)

    def htlc_list(self, status: str = None, hashlock: str = None) -> List[Dict]:
        """
        List HTLCs.

        Args:
            status: Filter by status (active, claimed, refunded)
            hashlock: Filter by hashlock

        Returns:
            List of HTLC records
        """
        args = []
        if status:
            args.append(status)
        if hashlock:
            args.append(hashlock)
        return self._call("htlc_list", *args)

    def htlc_get(self, htlc_outpoint: str) -> Optional[Dict]:
        """Get HTLC details."""
        return self._call("htlc_get", htlc_outpoint)

    def htlc_verify(self, preimage: str, hashlock: str) -> dict:
        """Verify preimage matches hashlock."""
        result = self._call("htlc_verify", preimage, hashlock)
        return result if result else {"valid": False, "error": "RPC failed"}

    def htlc_extract_preimage(self, txid: str) -> Optional[str]:
        """Extract preimage from a claim transaction."""
        result = self._call("htlc_extract_preimage", txid)
        return result.get("preimage") if result else None

    # =========================================================================
    # HTLC3S Operations (3-secret FlowSwap)
    # =========================================================================

    def htlc3s_generate(self) -> Dict:
        """Generate 3 secrets + hashlocks for FlowSwap."""
        return self._call("htlc3s_generate")

    def htlc3s_create(self, receipt_outpoint: str, hashlock_user: str,
                      hashlock_lp1: str, hashlock_lp2: str,
                      claim_address: str, expiry_blocks: int = 288,
                      template_commitment: str = None,
                      covenant_dest_address: str = None) -> Dict:
        """
        Lock M1 receipt in 3-secret HTLC.

        Args:
            receipt_outpoint: M1 receipt to lock (txid:vout)
            hashlock_user: SHA256 hash of user's secret (64 hex)
            hashlock_lp1: SHA256 hash of LP1's secret (64 hex)
            hashlock_lp2: SHA256 hash of LP2's secret (64 hex)
            claim_address: Address that can claim with 3 preimages
            expiry_blocks: Blocks until refund (default 288)
            template_commitment: C3 covenant hash (64 hex) for per-leg mode
            covenant_dest_address: LP_OUT address forced by covenant

        Returns:
            {"txid": "...", "htlc_outpoint": "txid:0", "amount": ..., "expiry_height": ...}
        """
        args = [receipt_outpoint, hashlock_user, hashlock_lp1, hashlock_lp2,
                claim_address, expiry_blocks]
        if template_commitment and covenant_dest_address:
            args.extend([template_commitment, covenant_dest_address])
        return self._call("htlc3s_create", *args)

    def htlc3s_claim(self, htlc_outpoint: str, preimage_user: str,
                     preimage_lp1: str, preimage_lp2: str) -> Dict:
        """
        Claim 3-secret HTLC with all 3 preimages.

        Args:
            htlc_outpoint: HTLC3S outpoint (txid:vout)
            preimage_user: User's preimage (64 hex)
            preimage_lp1: LP1's preimage (64 hex)
            preimage_lp2: LP2's preimage (64 hex)

        Returns:
            {"txid": "...", "receipt_outpoint": "txid:0", "amount": ...}
        """
        return self._call("htlc3s_claim", htlc_outpoint,
                          preimage_user, preimage_lp1, preimage_lp2)

    def htlc3s_refund(self, htlc_outpoint: str) -> Dict:
        """Refund expired 3-secret HTLC."""
        return self._call("htlc3s_refund", htlc_outpoint)

    def htlc3s_get(self, htlc_outpoint: str) -> Optional[Dict]:
        """Get 3S HTLC details."""
        try:
            return self._call("htlc3s_get", htlc_outpoint)
        except RuntimeError:
            return None

    def htlc3s_compute_c3(self, amount_sats: int, dest_address: str) -> Dict:
        """
        Compute C3 template hash for per-leg covenant.

        Args:
            amount_sats: M1 amount in sats
            dest_address: LP_OUT destination address (P2PKH)

        Returns:
            {"template_hash": "hex", "output_amount": float, "fee": float}
        """
        return self._call("htlc3s_compute_c3", amount_sats, dest_address)

    def htlc3s_list(self, status: str = None) -> List[Dict]:
        """List 3S HTLCs, optionally filtered by status."""
        if status:
            return self._call("htlc3s_list", status)
        return self._call("htlc3s_list")

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

    def get_finality_status(self) -> Dict:
        """Get HU finality status."""
        return self._call("getfinalitystatus")

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
        """Get raw transaction."""
        try:
            return self._call("getrawtransaction", txid, verbose)
        except RuntimeError:
            return None

    def send_raw_transaction(self, hex_tx: str) -> str:
        """Broadcast raw transaction."""
        return self._call("sendrawtransaction", hex_tx)

    def abandon_transaction(self, txid: str) -> bool:
        """Abandon a transaction (mark as not relayable)."""
        try:
            self._call("abandontransaction", txid)
            return True
        except RuntimeError:
            return False
