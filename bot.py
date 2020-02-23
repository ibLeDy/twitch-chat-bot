import argparse
import asyncio
import datetime
import json
import re
import sys
import traceback
from typing import Callable
from typing import List
from typing import Match
from typing import NamedTuple
from typing import NoReturn
from typing import Optional
from typing import Pattern
from typing import Tuple

import aiohttp


HOST = 'irc.chat.twitch.tv'
PORT = 6697

MSG_RE = re.compile('^:([^!]+).* PRIVMSG #[^ ]+ :([^\r]+)')
PRIVMSG = 'PRIVMSG #{channel} :{msg}\r\n'
SEND_MSG_RE = re.compile('^PRIVMSG #[^ ]+ :(?P<msg>[^\r]+)')


class Config(NamedTuple):
    username: str
    channel: str
    oauth_token: str
    client_id: str

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}('
            f'username={self.username!r}, '
            f'channel={self.channel!r}, '
            f'oauth_token={"***"!r}, '
            f'client_id={"***"!r}, '
            f')'
        )


def esc(s: str) -> str:
    return s.replace('{', '{{').replace('}', '}}')


async def send(writer: asyncio.StreamWriter, msg: str, *, quiet: bool = False) -> None:  # noqa: E501
    if not quiet:
        print(f'< {msg}', end='', flush=True, file=sys.stderr)
    writer.write(msg.encode())
    return await writer.drain()


async def recv(reader: asyncio.StreamReader, *, quiet: bool = False) -> bytes:
    data = await reader.readline()
    if not quiet:
        sys.stderr.buffer.write(b'> ')
        sys.stderr.buffer.write(data)
        sys.stderr.flush()
    return data


class Response:
    async def __call__(self, config: Config) -> Optional[str]:
        return None


class CmdResponse(Response):
    def __init__(self, cmd: str) -> None:
        self.cmd = cmd

    async def __call__(self, config: Config) -> Optional[str]:
        return self.cmd


class MessageResponse(Response):
    def __init__(self, match: Match[str], msg_fmt: str) -> None:
        self.match = match
        self.msg_fmt = msg_fmt

    async def __call__(self, config: Config) -> Optional[str]:
        params = self.match.groupdict()
        params['msg'] = self.msg_fmt.format(**params)
        return PRIVMSG.format(**params)


Callback = Callable[[Match[str]], Response]
HANDLERS: List[Tuple[Pattern[str], Callable[[Match[str]], Response]]]
HANDLERS = []


def handler(*prefixes: str, flags: re.RegexFlag = re.U) -> Callable[[Callback], Callback]:  # noqa: E501
    def handler_decorator(func: Callback) -> Callback:
        for prefix in prefixes:
            HANDLERS.append((re.compile(prefix + '\r\n$', flags=flags), func))
        return func
    return handler_decorator


def handle_message(*message_prefixes: str, flags: re.RegexFlag = re.U) -> Callable[[Callback], Callback]:  # noqa: E501
    return handler(
        *(
            f'^:(?P<user>[^!]+).* '
            f'PRIVMSG #(?P<channel>[^ ]+) '
            f':(?P<msg>{message_prefix}.*)'
            for message_prefix in message_prefixes
        ), flags=flags,
    )


@handler('^PING (.*)')
def pong(match: Match[str]) -> Response:
    return CmdResponse(f'PONG {match.group(1)}\r\n')


class UptimeResponse(Response):
    async def __call__(self, config: Config) -> Optional[str]:
        url = f'https://api.twitch.tv/helix/streams?user_login={config.channel}'  # noqa: E501
        headers = {'Client-ID': config.client_id}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                data = json.loads(text)['data']

                if not data:
                    msg = 'not currently streaming!'
                    return PRIVMSG.format(channel=config.channel, msg=msg)

                start_time_s = data[0]['started_at']
                start_time = datetime.datetime.strptime(
                    start_time_s, '%Y-%m-%dT%H:%M:%SZ',
                )
                elapsed = (datetime.datetime.utcnow() - start_time).seconds

                parts = []
                for n, unit in (
                        (60 * 60, 'hours'),
                        (60, 'minutes'),
                        (1, 'seconds'),
                ):
                    if elapsed // n:
                        parts.append(f'{elapsed // n} {unit}')
                    elapsed %= n
                msg = f'streaming for: {", ".join(parts)}'
                return PRIVMSG.format(channel=config.channel, msg=msg)


@handle_message('!uptime')
def cmd_uptime(match: Match[str]) -> Response:
    return UptimeResponse()


@handle_message('PING')
def msg_ping(match: Match[str]) -> Response:
    _, _, msg = match.groups()
    _, _, rest = msg.partition(' ')
    return MessageResponse(match, f'PONG {esc(rest)}')


def dt_str() -> str:
    dt_now = datetime.datetime.now()
    return f'[{dt_now.hour:02}:{dt_now.minute:02}]'


async def amain(config: Config, *, quiet: bool) -> NoReturn:
    reader, writer = await asyncio.open_connection(HOST, PORT, ssl=True)

    await send(writer, f'PASS {config.oauth_token}\r\n', quiet=True)
    await send(writer, f'NICK {config.username}\r\n', quiet=quiet)
    await send(writer, f'JOIN #{config.channel}\r\n', quiet=quiet)

    while True:
        data = await recv(reader, quiet=quiet)
        msg = data.decode('UTF-8', errors='backslashreplace')

        msg_match = MSG_RE.match(msg)
        if msg_match:
            print(f'{dt_str()}<{msg_match[1]}> {msg_match[2]}')

        for pattern, handler in HANDLERS:
            match = pattern.match(msg)
            if match:
                try:
                    res = await handler(match)(config)
                except Exception as e:
                    traceback.print_exc()
                    res = PRIVMSG.format(
                        channel=config.channel,
                        msg=f'*** unhandled {type(e).__name__} -- see logs',
                    )
                if res is not None:
                    send_match = SEND_MSG_RE.match(res)
                    if send_match:
                        print(f'{dt_str()}<{config.username}> {send_match[1]}')
                    await send(writer, res, quiet=quiet)
                break
        else:
            if not quiet:
                print(f'UNHANDLED: {msg}', end='')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    with open(args.config) as f:
        config = Config(**json.load(f))

    asyncio.run(amain(config, quiet=not args.verbose))
    return 0


if __name__ == '__main__':
    exit(main())
