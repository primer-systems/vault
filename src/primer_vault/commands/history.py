"""
History command implementations.
"""

from typing import TYPE_CHECKING

from .result import CommandResult

if TYPE_CHECKING:
    from ..core import Vault
    from .handler import CommandHandler


class HistoryCommands:
    """History-related commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        """Route history subcommands."""
        if not args:
            return self._list(20)

        if args[0] in ("--help", "-h"):
            return self._help()

        subcmd = args[0].lower()

        if subcmd in ("clear", "list"):
            if subcmd == "list":
                limit = 20
                if len(args) > 1:
                    try:
                        limit = int(args[1])
                    except ValueError:
                        return CommandResult.fail("Usage: history list [limit]")
                return self._list(limit)
            return self._clear(force=("--yes" in args or "-y" in args))
        elif subcmd == "show":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("""history show - Display transaction details

Usage: history show <tx_id>

The tx_id can be a prefix (e.g., first 8 characters).""")
            if len(args) < 2:
                return CommandResult.fail("Usage: history show <tx_id>")
            return self._show(args[1])
        elif subcmd == "export":
            if "--help" in args or "-h" in args:
                return self._export_help()
            filename = args[1] if len(args) > 1 else None
            return self._export(filename)
        elif subcmd == "verify":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("""history verify - Verify a transaction on-chain

Usage: history verify <tx_id>

Verifies the transaction exists on the blockchain.""")
            if len(args) < 2:
                return CommandResult.fail("Usage: history verify <tx_id>")
            return self._verify(args[1])
        elif subcmd == "receipt":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("""history receipt - Get AP2-formatted receipt

Usage: history receipt <tx_id>

Returns the receipt in AP2 JSON format.""")
            if len(args) < 2:
                return CommandResult.fail("Usage: history receipt <tx_id>")
            return self._receipt(args[1])
        else:
            # Try to parse as limit
            try:
                limit = int(subcmd)
                return self._list(limit)
            except ValueError:
                return CommandResult.fail("Usage: history [limit] | history show <id> | history clear | history export | history verify <id> | history receipt <id>")

    def _help(self) -> CommandResult:
        """Show history command help."""
        help_text = """history - View transaction history

Subcommands:
  history [limit]        - List recent transactions (default: 20)
  history list [limit]   - Same as above
  history show <id>      - Show transaction details
  history export [file]  - Export transactions to CSV
  history verify <id>    - Verify transaction on-chain
  history receipt <id>   - Get AP2-formatted receipt
  history clear          - Clear all history"""
        return CommandResult.ok(help_text)

    def _list(self, limit: int) -> CommandResult:
        """List recent transactions."""
        txs = self.core.get_recent_transactions(limit)
        if not txs:
            return CommandResult.ok("No transactions.")

        lines = ["Recent Transactions:"]
        for tx in txs:
            tx_type = getattr(tx, 'type', 'x402')
            agent = tx.agent_name or 'unknown'
            status = tx.status

            if tx_type == 'trade':
                # Trade: show swap details
                sym_in = getattr(tx, 'symbol_in', '?')
                sym_out = getattr(tx, 'symbol_out', '?')
                amt_in = getattr(tx, 'amount_in', '?')
                lines.append(f"  {tx.id[:8]}  {agent}  {amt_in} {sym_in} → {sym_out}  [trade/{status}]")
            elif tx_type == 'transfer':
                # Transfer: show amount and recipient
                sym = getattr(tx, 'transfer_symbol', 'ETH')
                amt = getattr(tx, 'transfer_amount', '?')
                recip = tx.recipient[:10] + '…' if tx.recipient and len(tx.recipient) > 12 else (tx.recipient or '?')
                lines.append(f"  {tx.id[:8]}  {agent}  {amt} {sym} → {recip}  [transfer/{status}]")
            else:
                # x402: show USDG amount
                amount = tx.amount_micro / 1_000_000
                lines.append(f"  {tx.id[:8]}  {agent}  ${amount:.6f}  [x402/{status}]")

        return CommandResult.ok("\n".join(lines), data={"transactions": [
            {
                "id": tx.id,
                "type": getattr(tx, 'type', 'x402'),
                "agent_name": tx.agent_name,
                "amount": tx.amount_micro / 1_000_000,
                "status": tx.status,
            }
            for tx in txs
        ]})

    def _show(self, tx_id: str) -> CommandResult:
        """Show transaction details."""
        txs = self.core.get_recent_transactions(1000)
        match = None
        for tx in txs:
            if tx.id.startswith(tx_id):
                match = tx
                break

        if not match:
            return CommandResult.fail(f"Transaction not found: {tx_id}")

        tx_type = getattr(match, 'type', 'x402')
        lines = [
            f"Transaction: {match.id}",
            f"  Type:       {tx_type}",
            f"  Agent:      {match.agent_name or 'unknown'}",
            f"  Status:     {match.status}",
            f"  Network:    {match.network}",
            f"  Created:    {match.timestamp[:19] if match.timestamp else 'Unknown'}",
        ]

        if tx_type == 'trade':
            lines.extend([
                f"  Token In:   {getattr(match, 'symbol_in', '?')} ({getattr(match, 'token_in', '?')})",
                f"  Amount In:  {getattr(match, 'amount_in', '?')}",
                f"  Token Out:  {getattr(match, 'symbol_out', '?')} ({getattr(match, 'token_out', '?')})",
                f"  Out quoted: {getattr(match, 'amount_out_quoted', None) or '-'}",
                f"  Out filled: {getattr(match, 'amount_out', None) or 'pending'}",
                f"  Fee Tier:   {getattr(match, 'fee_tier', '?')} bps",
                f"  Pool:       {getattr(match, 'pool', '?') or 'unknown'}",
            ])
        elif tx_type == 'transfer':
            lines.extend([
                f"  Token:      {getattr(match, 'transfer_symbol', 'ETH')}",
                f"  Amount:     {getattr(match, 'transfer_amount', '?')}",
                f"  Recipient:  {match.recipient or 'unknown'}",
            ])
        else:  # x402
            amount = match.amount_micro / 1_000_000
            lines.extend([
                f"  Amount:     ${amount:.6f}",
                f"  Recipient:  {match.recipient or 'unknown'}",
                f"  Resource:   {getattr(match, 'resource', None) or 'unknown'}",
            ])

        if match.tx_hash:
            lines.append(f"  Tx Hash:    {match.tx_hash}")
        if match.wallet_address:
            lines.append(f"  Wallet:     {match.wallet_address}")

        return CommandResult.ok("\n".join(lines), data={
            "transaction": {
                "id": match.id,
                "type": tx_type,
                "agent_name": match.agent_name,
                "status": match.status,
                "network": match.network,
            }
        })

    def _clear(self, inputs: dict = None, force: bool = False) -> CommandResult:
        """Clear history with confirmation."""
        if force or (inputs and inputs.get("confirm") == "YES"):
            count = self.core.clear_transactions()
            return CommandResult.ok(f"Cleared {count} transaction(s).")

        # Need confirmation
        self.handler._set_pending(
            lambda inp, **ctx: self._clear(inp),
        )
        return CommandResult.need_input(
            "confirm",
            "Clear all transaction history? This cannot be undone.\nType YES to confirm:",
        )

    def _export_help(self) -> CommandResult:
        """Help for history export."""
        return CommandResult.ok("""history export - Export transactions to CSV

Usage: history export [filename]

Arguments:
  [filename]  Output file path. Defaults to transactions.csv

Example:
  history export
  history export ~/payments.csv""")

    def _export(self, filename: str = None) -> CommandResult:
        """Export transactions to CSV file."""
        import csv
        from pathlib import Path
        from datetime import datetime

        txs = self.core.get_recent_transactions(10000)  # Get all
        if not txs:
            return CommandResult.ok("No transactions to export.")

        # Default to cwd. resolve() ensures the output message always shows the absolute path
        # so the user knows exactly where the file landed regardless of caller context.
        if not filename:
            filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = Path(filename).expanduser().resolve()

        try:
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                # Header
                # 'In' is what the trade actually delivered; 'In (quoted)' is
                # what it was predicted to. Both, so the difference survives the
                # export - a spreadsheet showing only the quote would report an
                # estimate as a settled fact.
                writer.writerow([
                    'ID', 'Type', 'Agent', 'Out', 'In', 'In (quoted)', 'Status',
                    'Recipient', 'Network', 'Timestamp', 'Tx Hash'
                ])
                # Data
                for tx in txs:
                    tx_type = getattr(tx, 'type', 'x402')

                    # Format "Out" column based on type
                    if tx_type == 'trade':
                        out_val = f"{getattr(tx, 'amount_in', '')} {getattr(tx, 'symbol_in', '')}"
                    elif tx_type == 'transfer':
                        out_val = f"{getattr(tx, 'transfer_amount', '')} {getattr(tx, 'transfer_symbol', 'ETH')}"
                    else:
                        out_val = f"{tx.amount_micro / 1_000_000:.6f} USDG"

                    # Format "In" column based on type
                    quoted_val = ''
                    if tx_type == 'trade':
                        sym_out = getattr(tx, 'symbol_out', '')
                        in_val = f"{getattr(tx, 'amount_out', '') or '?'} {sym_out}"
                        quoted = getattr(tx, 'amount_out_quoted', None)
                        quoted_val = f"{quoted} {sym_out}" if quoted else ''
                    elif tx_type == 'transfer':
                        in_val = ""  # Transfers out don't receive anything
                    else:
                        in_val = getattr(tx, 'request_url', None) or getattr(tx, 'resource', '') or ''

                    writer.writerow([
                        tx.id,
                        tx_type,
                        getattr(tx, 'agent_name', '') or '',
                        out_val,
                        in_val,
                        quoted_val,
                        tx.status,
                        getattr(tx, 'recipient', '') or '',
                        getattr(tx, 'network', '') or '',
                        getattr(tx, 'timestamp', '') or '',
                        getattr(tx, 'tx_hash', '') or ''
                    ])

            return CommandResult.ok(
                f"Exported {len(txs)} transaction(s) to {filepath.resolve()}",
                data={"filename": str(filepath.resolve()), "count": len(txs)}
            )
        except Exception as e:
            return CommandResult.fail(f"Export failed: {e}")

    def _find_transaction(self, tx_id: str):
        """Find transaction by ID prefix."""
        txs = self.core.get_recent_transactions(10000)
        for tx in txs:
            if tx.id.startswith(tx_id):
                return tx
        return None

    def _verify(self, tx_id: str) -> CommandResult:
        """Verify a transaction on-chain."""
        tx = self._find_transaction(tx_id)
        if not tx:
            return CommandResult.fail(f"Transaction not found: {tx_id}")

        try:
            self.core.verify_transaction(tx)
            return CommandResult.ok(
                f"Verification started for {tx.id[:16]}...\n"
                "Check history show to see the verification status."
            )
        except Exception as e:
            return CommandResult.fail(f"Verification failed: {e}")

    def _receipt(self, tx_id: str) -> CommandResult:
        """Get AP2-formatted receipt for a transaction."""
        import json

        tx = self._find_transaction(tx_id)
        if not tx:
            return CommandResult.fail(f"Transaction not found: {tx_id}")

        receipt = self.core.get_receipt(tx.id)

        if receipt.get("error"):
            return CommandResult.fail(f"Receipt error: {receipt['error']}")

        # Format receipt as pretty JSON
        receipt_json = json.dumps(receipt, indent=2)

        return CommandResult.ok(
            f"Receipt for {tx.id[:16]}...:\n\n{receipt_json}",
            data={"receipt": receipt}
        )
