import pathlib
import uuid

import orjson
from fastapi import FastAPI, Header
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from redis import asyncio as aioredis

from adsb_api.utils.api_v2 import router as v2_router
from adsb_api.utils.dependencies import provider, redisVRS
from adsb_api.utils.models import ApiUuidRequest, PlaneList, PrettyJSONResponse
from adsb_api.utils.plausible import plausible
from adsb_api.utils.settings import REDIS_HOST

PROJECT_PATH = pathlib.Path(__file__).parent.parent.parent

description = """
The adsb.lol API is a free and open source
API for the [adsb.lol](https://adsb.lol) project.

## Usage
You can use the API by sending a GET request
to the endpoint you want to use.
The API will return a JSON response.

## Terms of Service
You can use the API for free.

In the future, I might add a rate limit to the API.

In the future, you will require an API key
which you can get by feeding to adsb.lol.

If you want to use the API for production purposes,
please contact me so I do not break your application by accident.

## License

The license for the API as well as all data ADSB.lol
makes public is [ODbL](https://opendatacommons.org/licenses/odbl/summary/).

This is the same license
[OpenStreetMap](https://www.openstreetmap.org/copyright) uses.
"""

app = FastAPI(
    title="adsb.lol API",
    description=description,
    version="0.0.1",
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/openapi.json",
    license_info={
        "name": "Open Data Commons Open Database License (ODbL) v1.0",
        "url": "https://opendatacommons.org/licenses/odbl/1-0/",
    },
)

app.include_router(v2_router)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory=PROJECT_PATH / "templates")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/favicon.ico")


@app.get("/docs", include_in_schema=False)
def docs_override():
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="adsb.lol API",
        swagger_favicon_url="/favicon.ico",
    )


@app.on_event("startup")
async def startup_event():
    await provider.startup()
    redis = aioredis.from_url(REDIS_HOST, encoding="utf8", decode_responses=True)
    FastAPICache.init(RedisBackend(redis), prefix="api")
    redisVRS.redis_connection_string = REDIS_HOST
    await redisVRS.connect()
    await redisVRS.dispatch_background_task()


@app.on_event("shutdown")
async def shutdown_event():
    await provider.shutdown()
    await redisVRS.shutdown()


@app.get("/api/0/receivers", response_class=PrettyJSONResponse, include_in_schema=False)
async def receivers():
    return provider.beast_receivers


@app.get(
    "/api/0/mlat-server/0A/sync.json",
    response_class=PrettyJSONResponse,
    include_in_schema=False,
)
async def mlat_receivers():
    return provider.mlat_sync_json


@app.get(
    "/api/0/mlat-server/totalcount.json",
    response_class=PrettyJSONResponse,
    include_in_schema=False,
)
async def mlat_totalcount_json():
    return provider.mlat_totalcount_json


@app.post("/api/0/uuid", response_class=PrettyJSONResponse, include_in_schema=False)
async def post_uuid(data: ApiUuidRequest):
    generated_uuid = str(uuid.uuid4())
    json_log = orjson.dumps({"uuid": generated_uuid, "data": data.dict()})
    print(json_log)
    return {"uuid": generated_uuid}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """
    Return metrics for Prometheus
    """
    metrics = [
        "adsb_api_beast_total_receivers {}".format(len(provider.beast_receivers)),
        "adsb_api_beast_total_clients {}".format(len(provider.beast_clients)),
        "adsb_api_mlat_total {}".format(len(provider.mlat_sync_json)),
        "adsb_api_aircraft_total {}".format(provider.aircraft_totalcount),
    ]
    return Response(content="\n".join(metrics), media_type="text/plain")


@app.get("/api/0/me", response_class=PrettyJSONResponse, tags=["v0"])
async def api_me(
    x_original_forwarded_for: str | None = Header(default=None, include_in_schema=False)
):
    client_ip = x_original_forwarded_for
    my_beast_clients = provider.get_clients_per_client_ip(client_ip)
    mlat_clients = provider.mlat_clients_to_list(client_ip)
    response = {
        "feeding": {
            "beast": len(my_beast_clients) > 0,
            "mlat": len(mlat_clients) > 0,
        },
        "clients": {
            "beast": my_beast_clients,
            "mlat": mlat_clients,
        },
        "client_ip": client_ip,
        "global": {
            "beast": len(provider.beast_clients),
            "mlat": len(provider.mlat_clients),
            "planes": provider.aircraft_totalcount,
        },
    }

    return response


@app.get(
    "/api/0/airport/{icao}",
    response_class=PrettyJSONResponse,
    tags=["v0"],
    description="Data by https://github.com/vradarserver/standing-data/",
)
async def api_airport(icao: str):
    """
    Return information about an airport.
    """
    return await redisVRS.get_airport(icao)


async def get_route_for_callsign_lat_lng(callsign: str, lat: str, lng: str):
    route = await redisVRS.get_route(callsign)
    if route["airport_codes"] != "unknown":
        a = 0
        is_plausible = False
        distance = 0
        print(f"==> {callsign}:", end=" ")
        while a < len(route["_airports"]) - 1:
            b = a + 1
            airportA = route["_airports"][a]
            airportB = route["_airports"][b]
            print(f"checking {airportA['iata']}-{airportB['iata']}", end=" ")
            is_plausible, distance = plausible(
                lat,
                lng,
                f"{airportA['lat']:.5f}",
                f"{airportA['lon']:.5f}",
                f"{airportB['lat']:.5f}",
                f"{airportB['lon']:.5f}",
            )
            if is_plausible:
                break
            a = b  # try the next pair in mult segment routes

        if not is_plausible:
            print(f"implausible {lat}/{lng} (dist {distance}nm)")
        else:
            print(" [ok]")
        route["plausible"] = is_plausible
    return route


@app.get(
    "/api/0/route/{callsign}/{lat}/{lng}",
    response_class=PrettyJSONResponse,
    tags=["v0"],
    description="Data by https://github.com/vradarserver/standing-data/",
)
async def api_route3(
    callsign: str,
    lat: str = None,
    lng: str = None,
):
    """
    Return information about a route and plane position.
    Return value includes a guess whether
    this is a plausible route,given plane position.
    """
    route = await get_route_for_callsign_lat_lng(callsign, lat, lng)
    headers = {"Access-Control-Allow-Origin": "*"}
    return PrettyJSONResponse(content=route, headers=headers)


@app.get(
    "/api/0/route/{callsign}",
    response_class=PrettyJSONResponse,
    tags=["v0"],
    description="Data by https://github.com/vradarserver/standing-data/",
)
async def api_route(
    callsign: str,
):
    """
    Return information about a route.
    """
    route = await redisVRS.get_route(callsign)
    headers = {"Access-Control-Allow-Origin": "*"}
    return PrettyJSONResponse(content=route, headers=headers)


@app.post(
    "/api/0/routeset",
    response_class=PrettyJSONResponse,
    tags=["v0"],
    description="""Look up routes for multiple planes at once.
    Data by https://github.com/vradarserver/standing-data/""",
)
async def api_routeset(planeList: PlaneList):
    """
    Return route information on a list of planes / positions
    """
    print(planeList)
    response = []
    for plane in planeList.planes:
        route = await get_route_for_callsign_lat_lng(
            plane.callsign, plane.lat, plane.lng
        )
        response.append(route)
    headers = {"Access-Control-Allow-Origin": "*"}
    return PrettyJSONResponse(content=response, headers=headers)


if __name__ == "__main__":
    print("Run with:")
    print("uvicorn app:app --host 0.0.0.0 --port 80")
    print("or for development:")
    print("uvicorn app:app --host 0.0.0.0 --port 80 --reload")