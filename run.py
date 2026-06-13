from waitress import serve

from stackarr import config, create_app

if __name__ == "__main__":
    app = create_app()
    print(f"Stackarr listening on :{config.PORT}{config.URL_BASE or ''}")
    serve(app, host="0.0.0.0", port=config.PORT, threads=8)
