# Vendored osnm-z

`src/osnm_z` is a copy of [zunmax/osnm-z](https://github.com/zunmax/osnm-z) at commit
`b001c82bde34e408ab6f21e6e8be470ca2f189ab` (MIT, see `src/osnm_z/LICENSE`). That commit was
reviewed before it was copied: private keys are only used for local signing, network traffic
goes only to OpenSea and the configured RPC, and transactions only target OpenSea's SeaDrop
contract with the NFT recipient set to the minting wallet.

Changes, all marked with `nft-ops` comments:

| File | Change |
| --- | --- |
| `hooks.py` | New. Context-local log sink and pre-broadcast gate. |
| `logging.py` | Each print helper first offers the message to the log sink (per-wallet dashboard logs). |
| `submission.py` | Calls the pre-broadcast gate right before `eth_sendRawTransaction` (STOP and spending limits). |

`tests/test_vendored_integrity.py` checks every other file byte-for-byte against the reviewed
commit, so any unreviewed change fails the test suite.
