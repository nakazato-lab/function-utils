"""Run an .ndn script in the Invoker process."""
import asyncio

from ndnc.interp.evaluator import Interpreter, make_ndn_app
from ndnc.parser.parser import parse


async def execute_script(source, base_dir):
    program = parse(source)
    interpreter = Interpreter(base_dir=base_dir)
    # Execute without an NDN connection if no Interest or remote function call is needed.
    if not any(interpreter._has_interest(statement.expr) for statement in program):
        return await interpreter.exec_in_context(program, None)

    # Each invocation owns its interpreter and NDN connection.
    app = make_ndn_app()
    interpreter.app = app
    interpreter._register_local_data_routes()
    result = None
    execution_task = None

    async def run():
        nonlocal result, execution_task
        execution_task = asyncio.current_task()
        try:
            result = await interpreter.exec_in_context(program, app)
        finally:
            app.shutdown()

    after_start = run()
    network_task = asyncio.create_task(app.main_loop(after_start=after_start))
    try:
        # Keep cancellation visible to the caller even if NDNApp handles it internally.
        await asyncio.shield(network_task)
    finally:
        app.shutdown()
        tasks = [network_task]
        if execution_task is not None:
            tasks.append(execution_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        after_start.close()
    return result
