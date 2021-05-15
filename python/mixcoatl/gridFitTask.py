"""
To Do:
    1. Add gridCalibTable information to Connections and setup optional Butler inclusion (runQuantum?).
"""
import numpy as np
from scipy.spatial import distance

import lsst.afw.table as afwTable
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.pipe.base.connectionTypes as cT
from lsst.pex.config import Field

from .sourcegrid import DistortedGrid, grid_fit, coordinate_distances

class GridFitConnections(pipeBase.PipelineTaskConnections, 
                         dimensions=("instrument", "exposure", "detector")):

    inputCat = cT.Input(
        doc="Source catalog produced by characterize spot task.",
        name='spotSrc',
        storageClass="SourceCatalog",
        dimensions=("instrument", "exposure", "detector")
    )
    bbox = cT.Input(
        doc="Bounding box for CCD.",
        name="postISRCCD.bbox",
        storageClass="Box2I",
        dimensions=("instrument", "exposure", "detector")
    )
    gridSourceCat = cT.Output(
        doc="Source catalog produced by grid fit task.",
        name="gridSpotSrc",
        storageClass="SourceCatalog",
        dimensions=("instrument", "exposure", "detector")
    )

class GridFitConfig(pipeBase.PipelineTaskConfig,
                    pipelineConnections=GridFitConnections):
    """Configuration for GridFitTask."""

    numRows = Field(
        dtype=int,
        default=49,
        doc="Number of grid rows."
    )
    numColumns = Field(
        dtype=int,
        default=49,
        doc="Number of grid columns."
    )
    varyTheta = Field(
        dtype=bool,
        default=True,
        doc="Vary theta parameter during model fit."
    )
    fitMethod = Field(
        dtype=str,
        default='least_squares',
        doc="Minimization method for model fit."
    )

class GridFitTask(pipeBase.PipelineTask):

    ConfigClass = GridFitConfig
    _DefaultName = "GridFitTask" 
    
    @pipeBase.timeMethod
    def run(self, inputCat, bbox, gridCalibTable=None):

        all_srcY = inputCat['slot_Centroid_y']
        all_srcX = inputCat['slot_Centroid_x']
        
        ## Mask sources by shape
        quality_mask = (inputCat['slot_Shape_xx'] > 0.1) \
                     * (inputCat['slot_Shape_xx'] < 50.)  \
                     * (inputCat['slot_Shape_yy'] > 0.1) \
                     * (inputCat['slot_Shape_yy'] < 50.)

        ## Mask sources by distance to neighbors
        indices, distances = coordinate_distances(all_srcY, all_srcX, all_srcY, all_srcX)
        outlier_mask = ((distances[:,1] < 100.) & (distances[:,1] > 40.)) & \
            ((distances[:,2] < 100.) & (distances[:,2] > 40.))

        mask = quality_mask & outlier_mask
        srcY = all_srcY[mask]
        srcX = all_srcX[mask]
        
        ## Optionally use normalized centroid shifts from calibration
        if gridCalibTable is not None:
            gridCalib = DistortedGrid.fromAstropy(gridCalibTable)
            normalized_shifts = (gridCalib.norm_dy, gridCalib.norm_dx)
        else:
            normalized_shifts = None
            
        ## Perform grid fit
        grid, result = grid_fit(srcY, srcX, self.config.numColumns, self.config.numRows,
                                vary_theta=self.config.varyTheta, normalized_shifts=normalized_shifts,
                                method=self.config.fitMethod, bbox=bbox)

        ## Construct source catalog with new columns
        schema = inputCat.getSchema()
        mapper = afwTable.SchemaMapper(schema)
        mapper.addMinimalSchema(schema, True)
        gridYCol = mapper.editOutputSchema().addField('spotgrid_y', type=float,
                                                      doc='Y-position for spot grid source.')
        gridXCol = mapper.editOutputSchema().addField('spotgrid_x', type=float,
                                                      doc='X-position for spot grid source.')
        normDYCol = mapper.editOutputSchema().addField('spotgrid_normalized_dy', type=float,
                                                       doc='Normalized shift from spot grid source in Y.')
        normDXCol = mapper.editOutputSchema().addField('spotgrid_normalized_dx', type=float,
                                                       doc='Normalized shift from spot grid source in X.')
        gridIndexCol = mapper.editOutputSchema().addField('spotgrid_index', type=np.int32,
                                                          doc='Index of corresponding spot grid source.')

        outputCat = afwTable.SourceCatalog(mapper.getOutputSchema())
        outputCat.extend(inputCat, mapper=mapper)

        ## Match grid to catalog
        gridY, gridX = grid.get_source_centroids(distorted=False) # what if distortions provided?
        match_indices, match_distances = coordinate_distances(srcY, srcX, gridY, gridX)

        ## Construct new column arrays
        numSrcs = all_srcY.shape[0]
        all_gridY = np.full(numSrcs, np.nan)
        all_gridX = np.full(numSrcs, np.nan)
        all_normDY = np.full(numSrcs, np.nan)
        all_normDX = np.full(numSrcs, np.nan)
        all_gridIndex = np.full(numSrcs, np.nan)

        all_gridY[mask] = gridY[match_indices[:, 0]]
        all_gridX[mask] = gridX[match_indices[:, 0]]
        all_gridIndex[mask] = match_indices[:, 0]        
        dy = all_srcY - all_gridY
        dx = all_srcX - all_gridX
        all_normDY = (np.sin(-grid.theta)*dx + np.cos(-grid.theta)*dy)/grid.ystep
        all_normDX = (np.cos(-grid.theta)*dx - np.sin(-grid.theta)*dy)/grid.xstep 

        ## Assign new column arrays to catalog
        outputCat[gridYCol][:] = all_gridY
        outputCat[gridXCol][:] = all_gridX
        outputCat[normDYCol][:] = all_normDY
        outputCat[normDXCol][:] = all_normDX
        outputCat[gridIndexCol][:] = all_gridIndex
        
        ## Add grid parameters to metadata
        md = inputCat.getMetadata()
        md.add('GRID_X0', grid.x0)
        md.add('GRID_Y0', grid.y0)
        md.add('GRID_THETA', grid.theta)
        md.add('GRID_XSTEP', grid.xstep)
        md.add('GRID_YSTEP', grid.ystep)
        md.add('GRID_NCOLS', grid.ncols)
        md.add('GRID_NROWS', grid.nrows)
        outputCat.setMetadata(md)

        return pipeBase.Struct(gridSourceCat=outputCat)
