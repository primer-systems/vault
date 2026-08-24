# Security notes

Reference for how Vault protects key material and where the platform limits
that protection. For the encryption and KDF details see `wallet/crypto.py`.

## Copying secrets to the clipboard

Seed phrases and private keys can be copied from the wallet dialogs. Two
exposures are handled separately, because they need different mechanisms:

- **The live clipboard** is cleared automatically 60 seconds after the copy,
  provided it still holds the copied text (a later copy of something else is
  left alone). Implemented in `copy_sensitive_to_clipboard`
  (`ui/dialogs.py`).

- **Windows Clipboard History (Win+V) and Cloud Clipboard sync** are not
  reachable by that clear: once Windows captures a copy into the history list
  it persists there, and with sync enabled it is uploaded to the user's
  Microsoft account. Clearing the live clipboard does not remove it.

  To keep a secret out of both, the copy is marked at the moment it is set with
  the documented exclusion formats — `ExcludeClipboardContentFromMonitorProcessing`,
  `CanIncludeInClipboardHistory`, and `CanUploadToCloudClipboard`
  (`_set_clipboard_excluding_history`). This is the mechanism password managers
  use.

### Limits

- The exclusion is best effort. If the clipboard cannot be opened, or the
  platform does not honour the formats, the code falls back to an ordinary
  copy; the secret is then subject to whatever clipboard history the OS keeps.
- The exclusion applies only to copies Vault makes. A user who re-copies a
  revealed phrase by selecting the text themselves bypasses it.
- macOS and Linux clipboard managers have their own history mechanisms that
  Vault does not attempt to control; the 60-second live-clipboard clear still
  applies.

The safe path on any platform is to avoid copying a recovery phrase at all —
write it down from the screen — and to prefer a hardware wallet, which never
exposes key material to the host.

## Keystore file permissions

On Linux and macOS, Vault writes the wallet and its data files mode 0600 (owner
read/write only), applied to the temporary file before the atomic rename so
there is no readable window.

On Windows, Vault sets no ACL of its own; a file inherits the ACL of the folder
it sits in. This is adequate where the folder is already user-private:

- A `pip install` stores data under `%LOCALAPPDATA%\Primer\Vault`, whose
  inherited ACL is SYSTEM / Administrators / the owning user only.
- Any folder inside the user profile (Documents, Desktop) is likewise
  user-private.

It is **not** adequate for a portable install placed at a drive root (e.g.
`C:\Vault`) or on FAT32/exFAT removable media, where other local accounts —
or anyone with the stick — can read the folder. The keystore is still
encrypted, so a strong password remains the barrier; but the encrypted file,
the payment history and the agent registry are then copyable by others. Keep
the data folder in your user profile on a shared machine, or use a Ledger,
whose keys never touch the file.

## One wallet file, one Vault at a time

Vault prevents two instances from running against the same data directory. It
does not prevent two *separate* installs — each with its own data directory,
which is a supported setup — from both opening the *same* wallet file placed
outside either data directory (for example a wallet kept on a USB stick and
opened from two installs at once).

Each open instance holds the wallet in memory and writes the whole file on
every change, so in that specific arrangement the two can overwrite each
other's saves, and a seed added in one can be lost when the other saves. Keep
a wallet file open in one Vault at a time. Everything on disk stays encrypted;
what is at stake is a just-added seed that was not yet written down.

## Dust trades and gas (trading policy)

The trading policy caps value: a per-trade maximum and a daily volume limit,
both in dollars. It does not cap the *number* of trades. A trade worth almost
nothing — a one-wei ETH→WETH wrap, say — has a notional near zero, so it slips
under a dollar auto-approve threshold and barely moves the daily volume figure,
yet each one is a real transaction the wallet pays gas for.

An agent that has been given auto-approve (not the default) and is malicious or
compromised can therefore repeat such trades to spend the wallet's ETH on gas,
down to the policy's minimum-ETH reserve where trading stops. No tokens or keys
are exposed; the loss is bounded to gas above that reserve. If you enable
auto-approve for trading, do it only for an agent you trust. A non-dollar
control (a minimum notional, or a per-day transaction budget) is planned.

## Decrypted key material in memory

While a wallet is unlocked, its seed phrases and private keys are held
decrypted in the process for the session, and Python cannot zero the immutable
`str`/`bytes` that hold them. Locking the wallet drops the references; the
auto-lock timer does this after inactivity. Anything able to read the process
memory of an unlocked wallet can recover key material, so the unlocked window
is the exposure to minimise.
