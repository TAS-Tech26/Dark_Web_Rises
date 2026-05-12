import asyncio

async def main():
    HOST = "127.0.0.1"
    PORT = 65432

    reader, writer = await asyncio.open_connection(HOST, PORT)

    message = f"Hello from client".encode('utf-8')

    writer.write(message)

    await writer.drain()

    data = await reader.read(1024)
    print(f"recieved from server: {data.decode('utf-8')}")

    writer.close()

    await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())

