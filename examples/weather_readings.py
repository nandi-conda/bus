"""Made-up weather readings shared by the bus weather proc (weather.py) and the HTTPS one
(weather_service.py). A real proc would call a weather API."""


def reading(place, tick=0):
    if not isinstance(place, str) or not place:
        raise ValueError("place must be a non-empty string")
    return {"temp_c": sum(place.encode()) % 20 + tick % 5, "condition": "cloudy"}
