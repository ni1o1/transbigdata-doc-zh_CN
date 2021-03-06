'''
BSD 3-Clause License

Copyright (c) 2021, Qing Yu
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''

import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import LineString
import shapely
from .preprocess import (
    clean_same,
    clean_outofshape,
    id_reindex
)


def busgps_arriveinfo(data, line, stop, col=[
        'VehicleId', 'GPSDateTime', 'lon', 'lat', 'stopname'],
        stopbuffer=200, mintime=300, project_epsg=2416,
        timegap=1800, method='project', projectoutput=False):
    '''
    识别公交到离站信息

    输入公交GPS数据、公交线路与站点的GeoDataFrame，该方法能够识别公交的到离站信息

    Parameters
    -------
    data : DataFrame
        公交GPS数据，单一公交线路，且需要含有车辆ID、GPS时间、经纬度（wgs84）
    line : GeoDataFrame
        公交线型的GeoDataFrame数据，单一公交线路
    stop : GeoDataFrame
        公交站点的GeoDataFrame数据
    col : List
        列名，按[车辆ID,时间,经度,纬度，站点名称字段]的顺序
    stopbuffer : number
        米，站点的一定距离范围，车辆进入这一范围视为到站，离开则视为离站
    mintime : number
        秒，短时间内公交再次到站则需要与前一次的到站数据结合一起计算到离站时间，该参数设置阈值
    project_epsg : number
        匹配时会将数据转换为投影坐标系以计算距离，这里需要给定投影坐标系的epsg代号
    timegap : number
        秒，清洗数据用，多长时间车辆不出现，就视为新的车辆
    method : str
        公交运行图匹配方法，可选'project'或'dislimit'；
        project为直接匹配线路上最近点，匹配速度快；
        dislimit则需要考虑前面点位置，加上距离限制，匹配速度慢。
    projectoutput : bool
        是否输出投影后的数据

    Returns
    -------
    arrive_info : DataFrame
        公交到离站信息
    '''
    VehicleId, GPSDateTime, lon, lat, stopcol = col
    # Clean data
    print('Cleaning data', end='')
    line.set_crs(crs='epsg:4326', allow_override=True, inplace=True)
    line = line.to_crs(epsg=project_epsg)
    line_buffer = line.copy()
    line_buffer['geometry'] = line_buffer.buffer(200)
    line_buffer = line_buffer.to_crs(epsg=4326)
    print('.', end='')
    data = clean_same(data, col=[VehicleId, GPSDateTime, lon, lat])
    print('.', end='')
    data = clean_outofshape(data, line_buffer, col=[lon, lat], accuracy=500)
    print('.')
    data = id_reindex(data, VehicleId, timegap=timegap,
                      timecol=GPSDateTime, suffix='')

    print('Position matching', end='')
    # project data points onto bus LineString
    lineshp = line['geometry'].iloc[0]
    print('.', end='')
    data['geometry'] = gpd.points_from_xy(data[lon], data[lat])
    data = gpd.GeoDataFrame(data)
    data.set_crs(crs='epsg:4326', allow_override=True, inplace=True)
    print('.', end='')
    data = data.to_crs(epsg=project_epsg)
    print('.', end='')
    if method == 'project':
        data['project'] = data['geometry'].apply(lineshp.project)
    elif method == 'dislimit':
        tmps = []
        # Distance limit method
        for vid in data[VehicleId].drop_duplicates():
            print('.', end='')
            tmp = data[data[VehicleId] == vid].copy()
            i = 0
            tmp = tmp.sort_values(
                by=[VehicleId, GPSDateTime]).reset_index(drop=True)
            tmp['project'] = 0
            for i in range(len(tmp)-1):
                if i == 0:
                    proj = lineshp.project(tmp.iloc[i]['geometry'])
                    tmp.loc[i, 'project'] = proj
                else:
                    proj = tmp['project'].iloc[i]
                dis = tmp.iloc[i +
                               1]['geometry'].distance(tmp.iloc[i]['geometry'])
                if dis == 0:
                    proj1 = proj
                else:
                    proj2 = lineshp.project(tmp.iloc[i+1]['geometry'])
                    if abs(proj2-proj) > dis:
                        proj1 = np.sign(proj2-proj)*dis+proj
                    else:
                        proj1 = proj2
                tmp.loc[i+1, 'project'] = proj1
            tmps.append(tmp)
        data = pd.concat(tmps)
    print('.', end='')
    # Project bus stop to bus line
    stop = stop.to_crs(epsg=project_epsg)
    stop['project'] = stop['geometry'].apply(lineshp.project)
    print('.', end='')
    starttime = data[GPSDateTime].min()
    data['time_st'] = (data[GPSDateTime]-starttime).dt.total_seconds()
    BUS_project = data
    print('.')

    ls = []
    print('Matching arrival and leaving info...', end='')
    for car in BUS_project[VehicleId].drop_duplicates():
        print('.', end='')
        # Extract bus trajectory
        tmp = BUS_project[BUS_project[VehicleId] == car]
        if len(tmp) > 1:
            for stopname in stop[stopcol].drop_duplicates():
                # Get the stop position
                position = stop[stop[stopcol] == stopname]['project'].iloc[0]
                # Identify arrival and departure by intersection ofstop buffer
                # and line segment
                buffer_polygon = LineString([[0, position],
                                             [data['time_st'].max(), position]
                                             ]).buffer(stopbuffer)
                bus_linestring = LineString(tmp[['time_st', 'project']].values)
                line_intersection = bus_linestring.intersection(buffer_polygon)
                # Extract leave time
                if line_intersection.is_empty:
                    # If empty, no bus arrive
                    continue
                else:
                    if type(
                        line_intersection
                    ) == shapely.geometry.linestring.LineString:
                        arrive = [line_intersection]
                    else:
                        arrive = list(line_intersection)
                arrive = pd.DataFrame(arrive)
                arrive['arrivetime'] = arrive[0].apply(
                    lambda r: r.coords[0][0])
                arrive['leavetime'] = arrive[0].apply(
                    lambda r: r.coords[-1][0])
                # Filtering arrival information through time threshold
                a = arrive[['arrivetime']].copy()
                a.columns = ['time']
                a['flag'] = 1
                b = arrive[['leavetime']].copy()
                b.columns = ['time']
                b['flag'] = 0
                c = pd.concat([a, b]).sort_values(by='time')
                c['time1'] = c['time'].shift(-1)
                c['flag_1'] = ((c['time1']-c['time']) <
                               mintime) & (c['flag'] == 0)
                c['flag_2'] = c['flag_1'].shift().fillna(False)
                c['flag_3'] = c['flag_1'] | c['flag_2']
                c = c[-c['flag_3']]
                arrive_new = c[c['flag'] == 1][['time']].copy()
                arrive_new.columns = ['arrivetime']
                arrive_new['leavetime'] = list(c[c['flag'] == 0]['time'])
                arrive_new[stopcol] = stopname
                arrive_new[VehicleId] = car
                # Save data
                ls.append(arrive_new)
    # Concat data
    arrive_info = pd.concat(ls)
    arrive_info['arrivetime'] = starttime + \
        arrive_info['arrivetime'].apply(
            lambda r: pd.Timedelta(int(r), unit='s'))
    arrive_info['leavetime'] = starttime + \
        arrive_info['leavetime'].apply(
            lambda r: pd.Timedelta(int(r), unit='s'))
    if projectoutput:
        return arrive_info, data
    else:
        return arrive_info


def busgps_onewaytime(arrive_info, start, end,
                      col=['VehicleId', 'stopname',
                           'arrivetime', 'leavetime']):
    '''
    计算公交单程耗时

    输入到离站信息表arrive_info与站点信息表stop，计算单程耗时

    Parameters
    -------
    arrive_info : DataFrame
        公交到离站数据
    stop : GeoDataFrame
        公交站点的GeoDataFrame数据
    start : Str
        起点站名字
    end : Str
        终点站名字
    col : List
        字段列名[车辆ID,站点名称,到站时间,离站时间]

    Returns
    -------
    onewaytime : DataFrame
        公交单程耗时
    '''
    # For one direction
    # The information of start and end points is extracted and merged together
    # Arrival time of terminal
    [VehicleId, stopname, arrivetime, leavetime] = col
    arrive_info[arrivetime] = pd.to_datetime(arrive_info[arrivetime])
    arrive_info[leavetime] = pd.to_datetime(arrive_info[leavetime])
    a = arrive_info[arrive_info[stopname] ==
                    end][[arrivetime, stopname, VehicleId]]
    # Departure time of starting station
    b = arrive_info[arrive_info[stopname] ==
                    start][[leavetime, stopname, VehicleId]]
    a.columns = ['time', stopname, VehicleId]
    b.columns = ['time', stopname, VehicleId]
    # Concat data
    c = pd.concat([a, b])
    # After sorting, extract the travel time of each one-way trip
    c = c.sort_values(by=[VehicleId, 'time'])
    for i in c.columns:
        c[i+'1'] = c[i].shift(-1)
    c = c[(c[VehicleId] == c[VehicleId+'1']) &
          (c[stopname] == start) &
          (c[stopname+'1'] == end)]
    # Calculate the duration of the trip
    c['duration'] = (c['time1'] - c['time']).dt.total_seconds()
    c['shour'] = c['time'].dt.hour
    c['direction'] = start+'-'+end
    c1 = c.copy()
    # Do the same for the other direction
    a = arrive_info[arrive_info[stopname] ==
                    start][['arrivetime', stopname, VehicleId]]
    b = arrive_info[arrive_info[stopname] ==
                    end][['leavetime', stopname, VehicleId]]
    a.columns = ['time', stopname, VehicleId]
    b.columns = ['time', stopname, VehicleId]
    c = pd.concat([a, b])
    c = c.sort_values(by=[VehicleId, 'time'])
    for i in c.columns:
        c[i+'1'] = c[i].shift(-1)
    c = c[(c[VehicleId] == c[VehicleId+'1']) &
          (c[stopname] == end) & (c[stopname+'1'] == start)]
    c['duration'] = (c['time1'] - c['time']).dt.total_seconds()
    c['shour'] = c['time'].dt.hour
    c['direction'] = end+'-'+start
    c2 = c.copy()
    onewaytime = pd.concat([c1, c2])
    return onewaytime
