#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
from eth_account import Account
root=Path(__file__).resolve().parents[1]
allow_path=root/'config/wallets.json'
canon_path=Path(os.environ.get('NFT_CONFIG_PATH',str(root.parent/'shared/wallets/nft_config.json')))
try:
 allow=json.loads(allow_path.read_text());canon=json.loads(canon_path.read_text()).get('wallets',{})
 if not isinstance(allow,dict) or not allow:raise ValueError('allowlist kosong/invalid')
 for alias,item in allow.items():
  if alias not in canon:raise ValueError(f'alias {alias} tidak ada di canonical config')
  wallet=canon[alias];evm=wallet.get('evm') if isinstance(wallet.get('evm'),dict) else wallet
  addr=str(evm.get('address') or '');pk=str(evm.get('private_key') or '')
  if not addr or not pk:raise ValueError(f'alias {alias} tidak punya EVM pair lengkap')
  if str((item or {}).get('address') or '').lower()!=addr.lower():raise ValueError(f'address allowlist {alias} berbeda dari canonical')
  if Account.from_key(pk).address.lower()!=addr.lower():raise ValueError(f'private key EVM {alias} tidak cocok dengan address')
 print(f'wallet_allowlist_ok selected={len(allow)} aliases={",".join(allow)}')
except Exception as e:
 print(f'wallet_allowlist_invalid type={type(e).__name__} detail={e}',file=sys.stderr);raise SystemExit(1)
