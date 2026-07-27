import asyncio
import json
import os

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server

from dax_mcp import gmail

_server = Server('dax-gmail')

_TOOLS = [
    types.Tool(
        name='gmail_search',
        description=(
            'Search Gmail threads. Returns thread IDs and snippets. '
            'Use gmail_get_thread to read the full message chain.'
        ),
        inputSchema={
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'Gmail search query — same syntax as the Gmail search box. '
                                   'Examples: "subject:proposal from:alice", "after:2025/01/01 project"',
                },
                'max_results': {
                    'type': 'integer',
                    'description': 'Maximum number of threads to return (default 10, max 50)',
                    'default': 10,
                },
                'account': {
                    'type': 'string',
                    'description': 'dax-creds credential name. Defaults to DAX_CREDS_GMAIL.',
                },
            },
            'required': ['query'],
        },
    ),
    types.Tool(
        name='gmail_get_thread',
        description='Fetch the full message chain of a Gmail thread, including all replies. '
                    'Each message lists any attachments with their attachmentId and messageId '
                    '— pass those to gmail_get_attachment to retrieve the file.',
        inputSchema={
            'type': 'object',
            'properties': {
                'thread_id': {
                    'type': 'string',
                    'description': 'Thread ID returned by gmail_search',
                },
                'account': {
                    'type': 'string',
                    'description': 'dax-creds credential name. Defaults to DAX_CREDS_GMAIL.',
                },
            },
            'required': ['thread_id'],
        },
    ),
    types.Tool(
        name='gmail_get_attachment',
        description='Download a Gmail attachment. Returns the file content directly — '
                    'images and PDFs are returned in a format Claude can read. '
                    'Use gmail_get_thread first to find attachmentId and messageId.',
        inputSchema={
            'type': 'object',
            'properties': {
                'message_id': {
                    'type': 'string',
                    'description': 'messageId from the attachment entry in gmail_get_thread',
                },
                'attachment_id': {
                    'type': 'string',
                    'description': 'attachmentId from the attachment entry in gmail_get_thread',
                },
                'mime_type': {
                    'type': 'string',
                    'description': 'mimeType from the attachment entry (used to return the right content type)',
                },
                'filename': {
                    'type': 'string',
                    'description': 'filename from the attachment entry (used for context)',
                },
                'account': {
                    'type': 'string',
                    'description': 'dax-creds credential name. Defaults to DAX_CREDS_GMAIL.',
                },
            },
            'required': ['message_id', 'attachment_id'],
        },
    ),
]


def _get_token(account):
    from dax_creds.client import get_token
    name = account or os.environ.get('DAX_CREDS_GMAIL', '')
    if not name:
        raise RuntimeError('No Gmail credential configured (set account or DAX_CREDS_GMAIL)')
    return get_token(name)


@_server.list_tools()
async def _list_tools():
    return types.ListToolsResult(tools=_TOOLS)


@_server.call_tool()
async def _call_tool(name, arguments):
    arguments = arguments or {}
    account = arguments.get('account', '')

    try:
        token = _get_token(account)
    except Exception as e:
        return [types.TextContent(type='text', text=f'Credential error: {e}')]

    try:
        if name == 'gmail_search':
            query = arguments['query']
            max_results = min(int(arguments.get('max_results', 10)), 50)
            results = gmail.search_threads(token, query, max_results)
            return [types.TextContent(type='text', text=json.dumps(results, indent=2))]

        if name == 'gmail_get_thread':
            thread = gmail.get_thread(token, arguments['thread_id'])
            return [types.TextContent(type='text', text=json.dumps(thread, indent=2))]

        if name == 'gmail_get_attachment':
            import base64 as _b64
            raw_bytes = gmail.get_attachment(token, arguments['message_id'], arguments['attachment_id'])
            mime = arguments.get('mime_type', 'application/octet-stream')
            filename = arguments.get('filename', 'attachment')

            if mime.startswith('image/'):
                return [types.ImageContent(
                    type='image',
                    data=_b64.b64encode(raw_bytes).decode(),
                    mimeType=mime,
                )]

            if mime.startswith('text/'):
                return [types.TextContent(type='text', text=raw_bytes.decode('utf-8', errors='replace'))]

            if mime in ('application/pdf',
                        'application/msword',
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        'application/vnd.openxmlformats-officedocument.presentationml.presentation'):
                return [types.EmbeddedResource(
                    type='resource',
                    resource=types.BlobResourceContents(
                        uri=f'attachment://{filename}',
                        mimeType=mime,
                        blob=_b64.b64encode(raw_bytes).decode(),
                    ),
                )]

            # zip, binary, etc — return metadata only
            return [types.TextContent(
                type='text',
                text=f'Binary attachment "{filename}" ({mime}, {len(raw_bytes):,} bytes) — cannot display.',
            )]

        return [types.TextContent(type='text', text=f'Unknown tool: {name}')]

    except Exception as e:
        return [types.TextContent(type='text', text=f'Error: {e}')]


async def _main():
    async with stdio_server() as (read_stream, write_stream):
        await _server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name='dax-gmail',
                server_version='0.1.0',
                capabilities=_server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == '__main__':
    asyncio.run(_main())
