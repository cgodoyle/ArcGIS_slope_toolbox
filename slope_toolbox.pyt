# -*- coding: utf-8 -*-
import os
from pathlib import Path
import arcpy

from arcpy.management import GeneratePointsAlongLines, DefineProjection
from arcpy.sa import ExtractValuesToPoints

import numpy as np
from scipy.spatial import distance_matrix
import warnings

warnings.filterwarnings("ignore")

class Toolbox(object):
    def __init__(self):
        """Define the toolbox (the name of the toolbox is the name of the
        .pyt file)."""
        self.label = "Terrengkriterier Kvikkleireveileder"
        self.alias = "terrengkriterier"

        # List of tool classes associated with this toolbox
        self.tools = [SlopeTool]


class SlopeTool(object):
    def __init__(self):
        """Define the tool (tool name is the name of the class)."""
        self.label = "Compute slope with respect to sourcepoints"
        self.description = ("Compute Slope with respect to sourcepoints in order to check the terrain criteria "+\
                            "according to NVE's Kvikkleireveileder 1/2019 (cap. 3.2 step 3).")
        self.canRunInBackground = False

    def getParameterInfo(self):
        """Define parameter definitions"""
        lines = arcpy.Parameter(
            displayName="Source",
            name="lines",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Input",
        )
        lines.filter.list = ["Polyline", "Point"]

        in_raster = arcpy.Parameter(
            displayName="Input DEM raster",
            name="in_raster",
            datatype="DERasterDataset",
            parameterType="Required",
            direction="Input",
        )

        out_raster = arcpy.Parameter(
            displayName="Output Slopes/Terrain Criteria Raster",
            name="out_raster",
            datatype="DERasterDataset",
            parameterType="Required",
            direction="Output",
        )

        out_raster.value = os.path.join(
            Path(arcpy.env.workspace).parent.absolute(), "results.tif"
        )

        delta = arcpy.Parameter(
            displayName="Chainage distance (m)",
            name="delta",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        delta.value = 10

        h_min = arcpy.Parameter(
            displayName="h_min (m)",
            name="h_min",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        h_min.value = 5

        nodata = arcpy.Parameter(
            displayName="no data value",
            name="nodata",
            datatype="GPLong",
            parameterType="Required",
            direction="Input",
        )
        nodata.value = -9999

        params = [lines, in_raster, out_raster, delta, h_min, nodata]
        return params

    def isLicensed(self):
        """Set whether tool is licensed to execute."""
        return True

    def updateParameters(self, parameters):
        """Modify the values and properties of parameters before internal
        validation is performed.  This method is called whenever a parameter
        has been changed."""
        return

    def updateMessages(self, parameters):
        """Modify the messages created by internal validation for each tool
        parameter.  This method is called after internal validation."""
        return

    def execute(self, parameters, messages):
        """The source code of the tool."""

        messages.addMessage("Processing inputs")

        lines = parameters[0].valueAsText
        in_raster = parameters[1].valueAsText
        out_raster = parameters[2].valueAsText
        distance_chainage = parameters[3].value
        h_min = parameters[4].value
        nodata = parameters[5].value

        dem_points_spatial_ref = arcpy.Describe(in_raster).spatialReference
        desc_lines = arcpy.Describe(lines)

        messages.addMessage(f"Input feature is {desc_lines.shapeType}")

        if desc_lines.shapeType == "Polyline":

            # Convert line-shapefile to points, and points to numpy array
            points = GeneratePointsAlongLines(
                lines,
                r"memory\points",
                "DISTANCE",
                Distance=f"{int(distance_chainage)} meters",
            )
            points_z = ExtractValuesToPoints(points, in_raster, r"memory\points_z", "INTERPOLATE", "VALUE_ONLY")

        elif desc_lines.shapeType == "Point":
            points_z = lines
        else:
            messages.addErrorMessage("Feature must be Polylines or Point.")
            return

        # convert points to numpy array and get properties from attribute table
        points_np = arcpy.da.FeatureClassToNumPyArray(points_z, ["SHAPE@X", "SHAPE@Y", "ewd", "RASTERVALU"])
        # X- and Y-coordinates, ewd = estimated water depth, RASTERVALU = elevation (from raster)

        points_np = np.array([list(xx) for xx in points_np])

        # point's elevation is the difference between elevation and estimated water depth
        points_np = np.c_[points_np[:, 0], points_np[:, 1], points_np[:, 3] - points_np[:, 2]]

        # Get DEM properties
        dem_cols = arcpy.management.GetRasterProperties(in_raster, "COLUMNCOUNT")[0].replace(",", ".")
        dem_rows = arcpy.management.GetRasterProperties(in_raster, "ROWCOUNT")[0].replace(",", ".")
        dem_xmin = arcpy.management.GetRasterProperties(in_raster, "LEFT")[0].replace(",", ".")
        # dem_xmax = arcpy.management.GetRasterProperties(in_raster,"RIGHT")
        dem_ymin = arcpy.management.GetRasterProperties(in_raster, "BOTTOM")[0].replace(",", ".")
        dem_ymax = arcpy.management.GetRasterProperties(in_raster, "TOP")[0].replace(",", ".")
        dem_size_x = arcpy.management.GetRasterProperties(in_raster, "CELLSIZEX")[0].replace(",", ".")
        dem_size_y = arcpy.management.GetRasterProperties(in_raster, "CELLSIZEY")[0].replace(",", ".")

        # Input needed to get coordinates and to export to raster
        upperLeft = float(dem_xmin), float(dem_ymax)
        lowerLeft = arcpy.Point(float(dem_xmin), float(dem_ymin))

        # convert raster to a numpy matrix
        dem_array_np = arcpy.RasterToNumPyArray(in_raster, nodata_to_value=nodata)

        # get coordinates to every pixel in the raster
        dem_coords = compute_coordinates(dem_array_np, upperLeft, float(dem_size_x), float(dem_size_y))

        messages.addMessage("Computing slopes")

        # input variables to compute distance matrix
        xy_1 = dem_coords[:, :2]  # coordinates of all the pixels in the dem raster
        xy_2 = points_np[:, :2]  # coordinates of all the points in the points shapefile
        z1 = dem_coords[:, -1]  # elevation of all the pixels in the dem raster
        z2 = points_np[:, -1]  # elevation of all the points in the points shapefile

        # compute distance matrix, height matrix and slope ratio matrix
        distance_mtx = distance_matrix(xy_1, xy_2)
        height_mtx = z1[:, np.newaxis] - z2
        hl_ratio = height_mtx / distance_mtx

        # filter slopes lower than h_min, and compute the maximum slope of the matrix
        hl_ratio[height_mtx < h_min] = nodata
        max_slope = np.max(hl_ratio, axis=1)

        # TODO: add calculation of the 1:20-line
        # something like this:
        # depth_120 = np.max(height_mtx - distance_mtx / 20)
        # elevation_120 = z1 - depth_120

        # reshape array as the original dem
        out_array = max_slope.reshape(int(dem_rows), int(dem_cols), order="C")

        messages.addMessage("Exporting results")

        # export numpy to raster, define projection and save
        RasterBlock = arcpy.NumPyArrayToRaster(out_array, lowerLeft, int(dem_size_x), int(dem_size_y), nodata)
        DefineProjection(RasterBlock, dem_points_spatial_ref)
        RasterBlock.save(out_raster)

        messages.addMessage("Finished")

        return out_raster

    def postExecute(self, parameters):
        """This method takes place after outputs are processed and
        added to the display."""
        return


def compute_coordinates(arr:np.ndarray, border:tuple, resx:float, resy:float)->np.ndarray:
    """
    Compute coordinates of a raster array.
    Parameters:
        arr: 2D numpy array
        border: tuple of (x0, y0) coordinates of the lower left corner
        resx: x resolution
        resy: y resolution
    Returns:
        2D numpy array of shape (n, 3) with x, y, z coordinates
    """

    rows, cols = arr.shape
    x0, y0 = border
    coords = []

    for ii in range(rows):
        for jj in range(cols):
            x = x0 + jj * resx - resx / 2
            y = y0 - ii * resy + resy / 2
            z = arr[ii, jj]
            coords.append([x, y, z])

    return np.array(coords)
