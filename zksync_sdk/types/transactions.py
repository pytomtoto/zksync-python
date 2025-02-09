import abc
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from enum import Enum
from typing import List, Optional, Union, Tuple

from pydantic import BaseModel

from zksync_sdk.lib import ZkSyncLibrary
from zksync_sdk.serializers import (int_to_bytes, packed_amount_checked, packed_fee_checked,
                                    serialize_account_id,
                                    serialize_address, serialize_content_hash,
                                    serialize_nonce, serialize_timestamp,
                                    serialize_token_id, serialize_ratio_part)
from zksync_sdk.types.signatures import TxEthSignature, TxSignature

DEFAULT_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"

TokenLike = Union[str, int]

TRANSACTION_VERSION = 0x01


class ChangePubKeyTypes(Enum):
    onchain = "Onchain"
    ecdsa = "ECDSA"
    create2 = "CREATE2"


class RatioType(Enum):
    # ratio that represents the lowest denominations of tokens (wei for ETH, satoshi for BTC etc.)
    wei = 'Wei',
    # ratio that represents tokens themselves
    token = 'Token'


@dataclass
class ChangePubKeyEcdsa:
    batch_hash: bytes = b"\x00" * 32

    def encode_message(self):
        return self.batch_hash

    def dict(self, signature: str):
        return {"type":         "ECDSA",
                "ethSignature": signature,
                "batchHash":    f"0x{self.batch_hash.hex()}"}


@dataclass
class ChangePubKeyCREATE2:
    creator_address: str
    salt_arg: bytes
    code_hash: bytes

    def encode_message(self):
        return self.salt_arg

    def dict(self):
        return {"type":     "CREATE2",
                "saltArg":  f"0x{self.salt_arg.hex()}",
                "codeHash": f"0x{self.code_hash.hex()}"}


class Token(BaseModel):
    address: str
    id: int
    symbol: str
    decimals: int

    @classmethod
    def eth(cls):
        return cls(id=0,
                   address=DEFAULT_TOKEN_ADDRESS,
                   symbol="ETH",
                   decimals=18)

    def is_eth(self) -> bool:
        return self.symbol == "ETH" and self.address == DEFAULT_TOKEN_ADDRESS

    def decimal_amount(self, amount: int) -> Decimal:
        return Decimal(amount).scaleb(-self.decimals)

    def from_decimal(self, amount: Decimal) -> int:
        return int(amount.scaleb(self.decimals))

    def decimal_str_amount(self, amount: int) -> str:
        d = self.decimal_amount(amount)
        
        # Creates a string with `self.decimals` numbers after decimal point.
        # Prevents scientific notation (string values like '1E-8').
        # Prevents integral numbers having no decimal point in the string representation.
        d_str = f"{d:.{self.decimals}f}"

        d_str = d_str.rstrip("0")
        if d_str[-1] == ".":
            return d_str + "0"

        if '.' not in d_str:
            return d_str + '.0'

        return d_str


class Tokens(BaseModel):
    tokens: List[Token]

    def find_by_address(self, address: str) -> Optional[Token]:
        found_token = [token for token in self.tokens if token.address == address]
        if found_token:
            return found_token[0]
        else:
            return None

    def find_by_id(self, token_id: int) -> Optional[Token]:
        found_token = [token for token in self.tokens if token.id == token_id]
        if found_token:
            return found_token[0]
        else:
            return None

    def find_by_symbol(self, symbol: str) -> Optional[Token]:
        found_token = [token for token in self.tokens if token.symbol == symbol]
        if found_token:
            return found_token[0]
        else:
            return None

    def find(self, token: TokenLike) -> Token:
        result = None
        if isinstance(token, int):
            result = self.find_by_id(token)

        if isinstance(token, str):
            result = self.find_by_address(address=token)
            if result is None:
                result = self.find_by_symbol(symbol=token)
        return result


class EncodedTx(abc.ABC):
    @abc.abstractmethod
    def encoded_message(self) -> bytes:
        pass

    @abc.abstractmethod
    def human_readable_message(self) -> str:
        pass

    @abc.abstractmethod
    def tx_type(self) -> int:
        pass

    @abc.abstractmethod
    def dict(self):
        pass


@dataclass
class ChangePubKey(EncodedTx):
    account_id: int
    account: str
    new_pk_hash: str
    token: Token
    fee: int
    nonce: int
    valid_from: int
    valid_until: int
    eth_auth_data: Union[ChangePubKeyCREATE2, ChangePubKeyEcdsa] = None
    eth_signature: TxEthSignature = None
    signature: TxSignature = None

    def human_readable_message(self) -> str:
        message = f"Set signing key: {self.new_pk_hash.replace('sync:', '').lower()}"
        if self.fee:
            message += f"\nFee: {self.fee} {self.token.symbol}"
        return message

    def encoded_message(self) -> bytes:
        return b"".join([
            int_to_bytes(0xff - self.tx_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.account_id),
            serialize_address(self.account),
            serialize_address(self.new_pk_hash),
            serialize_token_id(self.token.id),
            packed_fee_checked(self.fee),
            serialize_nonce(self.nonce),
            serialize_timestamp(self.valid_from),
            serialize_timestamp(self.valid_until)
        ])

    def get_eth_tx_bytes(self) -> bytes:
        data = b"".join([
            serialize_address(self.new_pk_hash),
            serialize_nonce(self.nonce),
            serialize_account_id(self.account_id),
        ])
        if self.eth_auth_data is not None:
            data += self.eth_auth_data.encode_message()
        return data

    def get_auth_data(self, signature: str):
        if self.eth_auth_data is None:
            return {"type": "Onchain"}
        elif isinstance(self.eth_auth_data, ChangePubKeyEcdsa):
            return self.eth_auth_data.dict(signature)
        elif isinstance(self.eth_auth_data, ChangePubKeyCREATE2):
            return self.eth_auth_data.dict()

    def dict(self):
        return {
            "type":        "ChangePubKey",
            "accountId":   self.account_id,
            "account":     self.account,
            "newPkHash":   self.new_pk_hash,
            "fee_token":   self.token.id,
            "fee":         self.fee,
            "nonce":       self.nonce,
            "ethAuthData": self.eth_auth_data,
            "signature":   self.signature.dict(),
            "validFrom":   self.valid_from,
            "validUntil":  self.valid_until,
        }

    @classmethod
    def tx_type(cls):
        return 7


@dataclass
class Transfer(EncodedTx):
    account_id: int
    from_address: str
    to_address: str
    token: Token
    amount: int
    fee: int
    nonce: int
    valid_from: int
    valid_until: int
    signature: TxSignature = None

    def tx_type(self) -> int:
        return 5

    def human_readable_message(self) -> str:
        msg = ""

        if self.amount != 0:
            msg += f"Transfer {self.token.decimal_str_amount(self.amount)} {self.token.symbol} to: {self.to_address.lower()}\n"
        if self.fee != 0:
            msg += f"Fee: {self.token.decimal_str_amount(self.fee)} {self.token.symbol}\n"
        
        return msg + f"Nonce: {self.nonce}"

    def encoded_message(self) -> bytes:
        return b"".join([
            int_to_bytes(0xff - self.tx_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.account_id),
            serialize_address(self.from_address),
            serialize_address(self.to_address),
            serialize_token_id(self.token.id),
            packed_amount_checked(self.amount),
            packed_fee_checked(self.fee),
            serialize_nonce(self.nonce),
            serialize_timestamp(self.valid_from),
            serialize_timestamp(self.valid_until)
        ])

    def dict(self):
        return {
            "type":       "Transfer",
            "accountId":  self.account_id,
            "from":       self.from_address,
            "to":         self.to_address,
            "token":      self.token.id,
            "fee":        self.fee,
            "nonce":      self.nonce,
            "signature":  self.signature.dict(),
            "amount":     self.amount,
            "validFrom":  self.valid_from,
            "validUntil": self.valid_until,
        }


@dataclass
class Withdraw(EncodedTx):
    account_id: int
    from_address: str
    to_address: str
    amount: int
    fee: int
    nonce: int
    valid_from: int
    valid_until: int
    token: Token
    signature: TxSignature = None

    def tx_type(self) -> int:
        return 3

    def human_readable_message(self) -> str:
        msg = ""

        if self.amount != 0:
            msg += f"Withdraw {self.token.decimal_str_amount(self.amount)} {self.token.symbol} to: {self.to_address.lower()}\n"
        if self.fee != 0:
            msg += f"Fee: {self.token.decimal_str_amount(self.fee)} {self.token.symbol}\n"
        return msg + f"Nonce: {self.nonce}"

    def encoded_message(self) -> bytes:
        return b"".join([
            int_to_bytes(0xff - self.tx_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.account_id),
            serialize_address(self.from_address),
            serialize_address(self.to_address),
            serialize_token_id(self.token.id),
            int_to_bytes(self.amount, length=16),
            packed_fee_checked(self.fee),
            serialize_nonce(self.nonce),
            serialize_timestamp(self.valid_from),
            serialize_timestamp(self.valid_until)
        ])

    def dict(self):
        return {
            "type":       "Withdraw",
            "accountId":  self.account_id,
            "from":       self.from_address,
            "to":         self.to_address,
            "token":      self.token.id,
            "fee":        self.fee,
            "nonce":      self.nonce,
            "signature":  self.signature.dict(),
            "amount":     self.amount,
            "validFrom":  self.valid_from,
            "validUntil": self.valid_until,
        }


@dataclass
class ForcedExit(EncodedTx):
    initiator_account_id: int
    target: str
    token: Token
    fee: int
    nonce: int
    valid_from: int
    valid_until: int
    signature: TxSignature = None

    def tx_type(self) -> int:
        return 8

    def encoded_message(self) -> bytes:
        return b"".join([
            int_to_bytes(0xff - self.tx_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.initiator_account_id),
            serialize_address(self.target),
            serialize_token_id(self.token.id),
            packed_fee_checked(self.fee),
            serialize_nonce(self.nonce),
            serialize_timestamp(self.valid_from),
            serialize_timestamp(self.valid_until)
        ])

    def human_readable_message(self) -> str:
        message = f"ForcedExit {self.token.symbol} to: {self.target.lower()}\nFee: {self.token.decimal_str_amount(self.fee)} {self.token.symbol}\nNonce: {self.nonce}"
        return message

    def dict(self):
        return {
            "type":               "ForcedExit",
            "initiatorAccountId": self.initiator_account_id,
            "target":             self.target,
            "token":              self.token.id,
            "fee":                self.fee,
            "nonce":              self.nonce,
            "signature":          self.signature.dict(),
            "validFrom":          self.valid_from,
            "validUntil":         self.valid_until,
        }

@dataclass
class Order:
    account_id: int
    recipient: str
    nonce: int
    token_sell: Token
    token_buy: Token
    amount: int
    ratio: Fraction
    valid_from: int
    valid_until: int
    signature: TxSignature = None
    ethSignature: TxEthSignature = None

    def msg_type(self) -> int:
        return b'o'[0]

    def encoded_message(self) -> bytes:
        return b"".join([
            int_to_bytes(self.msg_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.account_id),
            serialize_address(self.recipient),
            serialize_nonce(self.nonce),
            serialize_token_id(self.token_sell.id),
            serialize_token_id(self.token_buy.id),
            serialize_ratio_part(self.ratio.numerator),
            serialize_ratio_part(self.ratio.denominator),
            packed_amount_checked(self.amount),
            serialize_timestamp(self.valid_from),
            serialize_timestamp(self.valid_until)
        ])

    def human_readable_message(self) -> str:
        if self.amount == 0:
            header = f'Limit order for {self.token_sell.symbol} -> {self.token_buy.symbol}'
        else:
            amount = self.token_sell.decimal_str_amount(self.amount)
            header = f'Order for {amount} {self.token_sell.symbol} -> {self.token_buy.symbol}'

        message = '\n'.join([
            header,
            f'Ratio: {self.ratio.numerator}:{self.ratio.denominator}',
            f'Address: {self.recipient.lower()}',
            f'Nonce: {self.nonce}'
        ])
        return message

    def dict(self):
        return {
            "accountId":        self.account_id,
            "recipient":        self.recipient,
            "nonce":            self.nonce,
            "tokenSell":        self.token_sell.id,
            "tokenBuy":         self.token_buy.id,
            "amount":           self.amount,
            "ratio":            (self.ratio.numerator, self.ratio.denominator),
            "validFrom":        self.valid_from,
            "validUntil":       self.valid_until,
            "signature":        self.signature.dict() if self.signature else None,
            "ethSignature":     self.ethSignature.dict() if self.ethSignature else None,
        }

@dataclass
class Swap(EncodedTx):
    submitter_id: int
    submitter_address: str
    amounts: Tuple[int, int]
    orders: Tuple[Order, Order]
    fee_token: Token
    fee: int
    nonce: int
    signature: TxSignature = None

    def tx_type(self) -> int:
        return 11

    def human_readable_message(self) -> str:
        if self.fee != 0:
            message =  f'Swap fee: {self.fee_token.decimal_str_amount(self.fee)} {self.fee_token.symbol}\n'
        else:
            message = ''
        message += f'Nonce: {self.nonce}'
        return message

    def encoded_message(self) -> bytes:
        order_bytes = b''.join([
            self.orders[0].encoded_message(),
            self.orders[1].encoded_message(),
        ])
        return b"".join([
            int_to_bytes(0xff - self.tx_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.submitter_id),
            serialize_address(self.submitter_address),
            serialize_nonce(self.nonce),
            ZkSyncLibrary().hash_orders(order_bytes),
            serialize_token_id(self.fee_token.id),
            packed_fee_checked(self.fee),
            packed_amount_checked(self.amounts[0]),
            packed_amount_checked(self.amounts[1]),
        ])

    def dict(self):
        return {
            "type":             "Swap",
            "submitterId":      self.submitter_id,
            "submitterAddress": self.submitter_address,
            "feeToken":         self.fee_token.id,
            "fee":              self.fee,
            "nonce":            self.nonce,
            "signature":        self.signature.dict() if self.signature else None,
            "amounts":          self.amounts,
            "orders":           (self.orders[0].dict(), self.orders[1].dict())
        }

@dataclass
class MintNFT(EncodedTx):
    creator_id: int
    creator_address: str
    content_hash: str
    recipient: str
    fee: int
    fee_token: Token
    nonce: int
    signature: TxSignature = None

    def tx_type(self) -> int:
        return 9

    def encoded_message(self) -> bytes:
        return b"".join([
            int_to_bytes(0xff - self.tx_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.creator_id),
            serialize_address(self.creator_address),
            serialize_content_hash(self.content_hash),
            serialize_address(self.recipient),
            serialize_token_id(self.fee_token.id),
            packed_fee_checked(self.fee),
            serialize_nonce(self.nonce),
        ])

    def human_readable_message(self) -> str:
        message = f"MintNFT {self.content_hash} for: {self.recipient.lower()}\nFee: {self.fee_token.decimal_str_amount(self.fee)} {self.fee_token.symbol}\nNonce: {self.nonce}"
        return message

    def dict(self):
        return {
            "type":               "MintNFT",
            "creatorId":          self.creator_id,
            "creatorAddress":     self.creator_address,
            "contentHash":        self.content_hash,
            "recipient":          self.recipient,
            "feeToken":           self.fee_token.id,
            "fee":                self.fee,
            "nonce":              self.nonce,
            "signature":          self.signature.dict(),
        }

@dataclass
class WithdrawNFT(EncodedTx):
    account_id: int
    from_address: str
    to_address: str
    fee_token: Token
    fee: int
    nonce: int
    valid_from: int
    valid_until: int
    token_id: int
    signature: TxSignature = None

    def tx_type(self) -> int:
        return 10

    def encoded_message(self) -> bytes:
        return b"".join([
            int_to_bytes(0xff - self.tx_type(), 1),
            int_to_bytes(TRANSACTION_VERSION, 1),
            serialize_account_id(self.account_id),
            serialize_address(self.from_address),
            serialize_address(self.to_address),
            serialize_token_id(self.token_id),
            serialize_token_id(self.fee_token.id),
            packed_fee_checked(self.fee),
            serialize_nonce(self.nonce),
            serialize_timestamp(self.valid_from),
            serialize_timestamp(self.valid_until)
        ])

    def human_readable_message(self) -> str:
        message = f"WithdrawNFT {self.token_id} to: {self.to_address.lower()}\nFee: {self.fee_token.decimal_str_amount(self.fee)} {self.fee_token.symbol}\nNonce: {self.nonce}"
        return message

    def dict(self):
        return {
            "type":                 "WithdrawNFT",
            "accountId":            self.account_id,
            "from":                 self.from_address,
            "to":                   self.to_address,
            "feeToken":             self.fee_token.id,
            "fee":                  self.fee,
            "nonce":                self.nonce,
            "validFrom":            self.valid_from,
            "validUntil":           self.valid_until,
            "token":                self.token_id,
            "signature":            self.signature.dict(),
        }

@dataclass
class TransactionWithSignature:
    tx: EncodedTx
    signature: TxEthSignature

    def dict(self):
        return {
            'tx':        self.tx.dict(),
            'signature': self.signature.dict(),
        }
