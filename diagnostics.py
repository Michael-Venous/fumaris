def console_log(message):
    try:
        print(message)
    except (OSError, ValueError):
        pass
