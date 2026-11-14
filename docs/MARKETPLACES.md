# Other marketplaces' drops — research notes (for a future update)

Goal: mint drops hosted outside OpenSea as smoothly as OpenSea drops, starting with
**Scatter** and **Kalos**. Today these work through *Mint → Any contract*, which reads the
contract on-chain. The notes below are what a dedicated adapter needs.

## Scatter (scatter.art) — confirmed from source code

Scatter's contracts are open source: `github.com/guminc/archetype` ("Solidity contracts for
Scatter's NFT launchpad"). Collections are clones of **Archetype** (ERC-721A, plus ERC-1155 and
404 variants) created by a Factory.

**Mint function (ERC-721A):**

```solidity
mint(Auth auth, uint256 quantity, address affiliate, bytes signature, MintConstraints constraints) payable
struct Auth { bytes32 key; bytes32[] proof; }
struct MintConstraints { address currency; uint128 maxCurrencyCost; uint256 maxNativeValue; uint256 minTotalMints; }
```

**Phases are called "invites".** Each one is stored on-chain under a `bytes32 key`:

```solidity
mapping(bytes32 => Invite) public invites;
struct Invite { uint128 price; uint32 start; uint32 end; uint32 limit; uint32 maxSupply;
                uint32 unitSize; address tokenAddress; bool isBlacklist; }
event Invited(bytes32 indexed key, bytes32 indexed cid);   // one per phase
```

- **Public phase:** the key is a small number (≤ 0xff) or `keccak256(tokenAddress)`. No proof
  is needed (`proof = []`). A protocol minimum fee also applies
  (`mintFeeRegistry.nativeMinimumFee()`).
- **Allowlist phase:** the key **is the Merkle root** of the allowed addresses. Each wallet
  needs its Merkle proof (leaf = `keccak256(abi.encodePacked(wallet))`, verified with Solady
  `MerkleProofLib`).
- **Exact price:** `computePrice(key, quantity, affiliateUsed)`.
- **Already minted:** `minted(wallet, key)`.
- **No affiliate:** `affiliate = 0x0` with an empty `signature`.
- **`constraints`:** set `maxNativeValue` to the value we send, which caps what can be charged.

**What an adapter can do automatically:**
1. Find the phases: read the `Invited` events (the keys), then `invites(key)` for price,
   start, end, limit and supply.
2. Detect the phase type: public versus allowlist, from the key.
3. Readiness per wallet: price via `computePrice`, the limit versus `minted(wallet, key)`,
   balance and gas.
4. Build the exact `mint(...)` call and simulate it before sending, as the existing engine does.

**Still open:** allowlist proofs. The contract only stores the root. Scatter's website supplies
proofs from its own backend, and that API is not documented, so it will not be guessed. Options
for the next stage:
- (a) If the project publishes its allowlist (a CSV of addresses), NFT Ops builds every wallet's
  proof locally and checks it against the on-chain root before use.
- (b) You paste a wallet's proof copied from the Scatter mint page.
Public phases need neither.

## Kalos — not confirmed yet

One source (an airdrop guide) describes Kalos as an NFT marketplace, launchpad ("Drops") and
DEX on **Kinetiq's Elysium** network (already in NFT Ops' network list). No public contract
source or documentation was found.

**Needed to build an adapter:** one or two Kalos drop contract addresses on Elysium. The plan is
to:
- read their verified source or ABI on the Elysium explorer;
- identify the mint function, phase storage and allowlist method (Merkle root, server signature
  or open);
- add a Kalos adapter like the Scatter one.

## Already handled today (any marketplace)

*Mint → Any contract* analyses a contract's ABI (verified source or one you paste) and lists
its mint functions, price, supply and sale state. You can then mint with your wallets: each one
is test-run first, can be scheduled, and is proven by the NFTs arriving. Contracts that need a
per-wallet server signature are recognised, but can only be minted once that signature can be
obtained legitimately.
