import argparse
import asyncio
import json

import httpx

from app.config.settings import Settings


async def run(args: argparse.Namespace) -> None:
    s = Settings()
    async with httpx.AsyncClient(base_url=s.api_base_url, timeout=45,
            headers={'Authorization': 'Bearer ' + s.admin_api_key.get_secret_value()}) as client:
        if args.command == 'join':
            response = await client.post('/api/meetings', json={
                'meeting_url': args.meeting_url, 'consent_confirmed': args.consent_confirmed})
        elif args.command == 'leave':
            response = await client.post(f'/api/meetings/{args.session_id}/leave')
        elif args.command in {'transcript', 'latency'}:
            response = await client.get(f'/api/meetings/{args.session_id}/{args.command}')
        elif args.command in {'chat', 'chat-metrics'}:
            suffix = 'chat/metrics' if args.command == 'chat-metrics' else 'chat'
            response = await client.get(f'/api/meetings/{args.session_id}/{suffix}')
        elif args.command == 'delete':
            response = await client.delete(f'/api/meetings/{args.session_id}')
        else:
            response = await client.get(f'/api/meetings/{args.session_id}')
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description='Control an authorized AI meeting participant')
    sub = parser.add_subparsers(dest='command', required=True)
    join = sub.add_parser('join')
    join.add_argument('--meeting-url', required=True)
    join.add_argument('--consent-confirmed', action='store_true', required=True)
    for command in ('leave', 'status', 'transcript', 'latency', 'chat', 'chat-metrics', 'delete'):
        sub.add_parser(command).add_argument('session_id')
    try:
        asyncio.run(run(parser.parse_args()))
    except httpx.HTTPError as exc:
        code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 'network'
        parser.exit(1, f'API request failed ({code}). Check server logs and configuration.\n')


if __name__ == '__main__':
    main()
