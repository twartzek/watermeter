from matplotlib import pyplot as plt
import numpy as np
from db import get_last_readings
import plotly.graph_objects as go


def runningWindowMaxFlowOutlierDetector(data):
    MAXFLOW = 0.02
    newData = []
    for i in range(1, len(data)):
        dt = (data[i]['time'] - data[i-1]['time']).total_seconds() / 60
        dy = abs(data[i]['totalconsumption'] - data[i-1]['totalconsumption'])
        if dy/dt > MAXFLOW:
            continue
        newData.append(data[i])
    return newData

def runningMedian(data, windowSize: int=5):
    newData = []
    for i in range(windowSize, len(data)):
        res = np.median([x['totalconsumption'] for x in data[i-windowSize:i]])
        newData.append({"time": data[i]['time'], "totalconsumption": res})
    return newData

def filterMissingDigits(data):
    newData = []
    for d in data:
        if d["totalconsumption"] < 600:
            continue
        newData.append({"time": d['time'], "totalconsumption": d["totalconsumption"]})

    return newData


# Get Data
readings = get_last_readings(800)
readings = sorted(readings, key=lambda x: x.time)
data = [{"time": x.time, "totalconsumption": x.totalconsumption} for x in readings]


# Filter data
datafilt = data
datafilt = filterMissingDigits(data)
datafilt = runningWindowMaxFlowOutlierDetector(datafilt)
datafilt = runningMedian(datafilt, 10)


# Plot Data
fig = go.Figure()
fig.add_trace(go.Scatter(x=[x['time'] for x in data], y=[x['totalconsumption'] for x in data],
                    mode='lines+markers',
                    name='lines'))
fig.add_trace(go.Scatter(x=[x['time'] for x in datafilt], y=[x['totalconsumption'] for x in datafilt],
                    mode='lines+markers',
                    name='lines+markers'))

fig.show()