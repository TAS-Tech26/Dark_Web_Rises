import asyncio

async def handle_client(reader, writer):
    addr = writer.get_extra_info('peername')
    print(f"\n{addr} connected")

    while True:
        data = await reader.read(1024)

        if not data:
            break

        print(f"{addr} says {data.decode('utf-8')}")

        writer.write(data)

        await writer.drain()

    print(f"\n{addr} disconnected")

    writer.close()
    await writer.wait_closed()


async def main():
    HOST = "127.0.0.1"
    PORT = 65432

    server = await asyncio.start_server(handle_client, HOST, PORT)

    print(f"server is listening on {HOST}:{PORT}")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())