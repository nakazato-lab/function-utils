# Function definitions

Python functions must define `execute(input_bytes, args)`:

```python
def execute(input_bytes, args):
    return str(int(args[0]) * 10)
```

- `input_bytes`: Binary input (`bytes`), or `None` when absent.
- `args`: A list of strings. Convert numeric arguments as needed.
- Return value: `bytes`, `str`, or `None`.

Put execution logic inside `execute`. Use its arguments and return value instead of `sys.stdin`, `sys.argv`, or standard output. See [compress_image.py](compress_image.py) for an image example.

Modules are loaded once and reused, so global state persists between calls.

`.ndn` functions remain NDN scripts and do not require `execute`.
