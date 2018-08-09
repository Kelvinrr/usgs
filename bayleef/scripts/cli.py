import errno
import json
import logging
import os
import re
import sys
import tarfile
from datetime import datetime
from glob import glob
from shutil import copyfile
from threading import Thread

import re
import fnmatch
import click
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

import gdal
import wget
from plio.io.io_gdal import GeoDataset

from pysbatch import *

from .. import api
from .. import ingest
from .. import utils
from .. import sql

LOG_FORMAT = '%(asctime)-15s ->> %(message)s'
logging.basicConfig(format=LOG_FORMAT)
logger = logging.getLogger('bayleef-cli')
logger.setLevel(logging.INFO)

def get_node(dataset, node=None):

    if node is None:

        cur_dir = os.path.dirname(os.path.realpath(__file__))
        data_dir = os.path.join(cur_dir, "..", "data")
        dataset_path = os.path.join(data_dir, "datasets.json")

        with open(dataset_path, "r") as f:
            datasets = json.loads(f.read())

        node = datasets[dataset].upper()

    return node


def to_coordinates(bounds):
    xmin, ymin, xmax, ymax = bounds

    return [[
        [xmin, ymin],
        [xmin, ymax],
        [xmax, ymax],
        [xmax, ymin],
        [xmin, ymin]
    ]]


def to_geojson_feature(entry):

    # TODO: This key may not be present in all datasets.
    bounds = list(map(float, entry.pop("sceneBounds").split(',')))

    coordinates = to_coordinates(bounds)

    return {
        "type": "Feature",
        "properties": entry,
        "geometry": {
            "type": "Polygon",
            "coordinates": coordinates
        }
    }


def to_geojson(result):
    gj = {
        'type': 'FeatureCollection'
    }

    if type(result['data']) is list:
        features = list(map(to_geojson_feature, result['data']))
    else:
        features = list(map(to_geojson_feature, result['data']['results']))
        for key in result['data']:
            if key == "results":
                continue
            gj[key] = result['data'][key]

    gj['features'] = features
    for key in result:
        if key == "data":
            continue
        gj[key] = result[key]

    return gj


def explode(coords):
    for e in coords:
        if isinstance(e, (float, int, long)):
            yield coords
            break
        else:
            for f in explode(e):
                yield f


def get_bbox(f):
    x, y = zip(*list(explode(f['geometry']['coordinates'])))
    return min(x), min(y), max(x), max(y)


api_key_opt = click.option("--api-key", help="API key returned from USGS servers after logging in.", default=None)
node_opt = click.option("--node", help="The node corresponding to the dataset (CWIC, EE, HDDS, LPVS).", default=None)


@click.group()
def bayleef():
    pass


@click.command()
@click.argument("username", envvar='USGS_USERNAME')
@click.argument("password", envvar='USGS_PASSWORD')
def login(username, password):
    """
    Login to the USGS EROs service.
    """
    api_key = api.login(username, password)
    click.echo(api_key)


@click.command()
def logout():
    click.echo(api.logout())


@click.command()
@click.argument("node")
@click.option("--start-date", help="Start date for when a scene has been acquired. In the format of yyyy-mm-dd")
@click.option("--end-date", help="End date for when a scene has been acquired. In the format of yyyy-mm-dd")
def datasets(node, start_date, end_date):
    data = api.datasets(None, node, start_date=start_date, end_date=end_date)
    click.echo(json.dumps(data))


@click.command()
@click.argument("dataset")
@click.argument("scene-ids", nargs=-1)
@node_opt
@click.option("--extended", is_flag=True, help="Probe for more metadata.")
@click.option('--geojson', is_flag=True)
@api_key_opt
def metadata(dataset, scene_ids, node, extended, geojson, api_key):
    """
    Request metadata.
    """
    if len(scene_ids) == 0:
        scene_ids = map(lambda s: s.strip(), click.open_file('-').readlines())

    node = get_node(dataset, node)
    result = api.metadata(dataset, node, scene_ids, extended=extended, api_key=api_key)

    if geojson:
        result = to_geojson(result)

    click.echo(json.dumps(result))


@click.command()
@click.argument("dataset")
@node_opt
def dataset_fields(dataset, node):
    node = get_node(dataset, node)
    data = api.dataset_fields(dataset, node)
    click.echo(json.dumps(data))


@click.command()
@click.argument("dataset")
@node_opt
@click.argument("aoi", default="-", required=False)
@click.option("--start-date", help="Start date for when a scene has been acquired. In the format of yyyy-mm-dd")
@click.option("--end-date", help="End date for when a scene has been acquired. In the format of yyyy-mm-dd")
@click.option("--lng", help="Longitude")
@click.option("--lat", help="Latitude")
@click.option("--dist", help="Radius - in units of meters - used to search around the specified longitude/latitude.", default=100)
@click.option("--lower-left", nargs=2, help="Longitude/latitude specifying the lower left of the search window")
@click.option("--upper-right", nargs=2, help="Longitude/latitude specifying the lower left of the search window")
@click.option("--where", nargs=2, multiple=True, help="Supply additional search criteria.")
@click.option('--geojson', is_flag=True)
@click.option("--extended", is_flag=True, help="Probe for more metadata.")
@api_key_opt
def search(dataset, node, aoi, start_date, end_date, lng, lat, dist, lower_left, upper_right, where, geojson, extended, api_key):
    """
    Search for images.
    """
    node = get_node(dataset, node)

    if aoi == "-":
        src = click.open_file('-')
        if not src.isatty():
            lines = src.readlines()

            if len(lines) > 0:

                aoi = json.loads(''.join([ line.strip() for line in lines ]))

                bbox = map(get_bbox, aoi.get('features') or [aoi])[0]
                lower_left = bbox[0:2]
                upper_right = bbox[2:4]

    if where:
        # Query the dataset fields endpoint for queryable fields
        resp = api.dataset_fields(dataset, node)

        def format_fieldname(s):
            return ''.join(c for c in s if c.isalnum()).lower()

        field_lut = { format_fieldname(field['name']): field['fieldId'] for field in resp['data'] }
        where = { field_lut[format_fieldname(k)]: v for k, v in where if format_fieldname(k) in field_lut }


    if lower_left:
        lower_left = dict(zip(['longitude', 'latitude'], lower_left))
        upper_right = dict(zip(['longitude', 'latitude'], upper_right))

    result = api.search(dataset, node, lat=lat, lng=lng, distance=dist, ll=lower_left, ur=upper_right, start_date=start_date, end_date=end_date, where=where, extended=extended, api_key=api_key)

    if geojson:
        result = to_geojson(result)

    print(json.dumps(result))


@click.command()
@click.argument("dataset")
@click.argument("scene-ids", nargs=-1)
@node_opt
@api_key_opt
def download_options(dataset, scene_ids, node, api_key):
    node = get_node(dataset, node)

    data = api.download_options(dataset, node, scene_ids)
    print(json.dumps(data))


@click.command()
@click.argument("dataset")
@click.argument("scene_ids", nargs=-1)
@click.option("--product", nargs=1, required=True)
@node_opt
@api_key_opt
def download_url(dataset, scene_ids, product, node, api_key):
    node = get_node(dataset, node)

    data = api.download(dataset, node, scene_ids, product)
    click.echo(json.dumps(data))


@click.command()
@click.argument("root")
@node_opt
def batch_download(root, node):
    """
    Download from search result.
    """
    def download_from_result(scene, root):
        scene_id = scene['entityId']
        temp_file = '{}.tar.gz'.format(scene_id)

        dataset = re.findall(r'dataset_name=[A-Z0-9_]*', scene['orderUrl'])[0]
        dataset = dataset.split('=')[1]

        path = utils.get_path(scene, root, dataset)
        if os.path.exists(path):
            logger.warning('{} already in cache, skipping'.format(path))
            return

        download_info = api.download(dataset, get_node(dataset, node), scene_id)
        download_url = download_info['data'][0]

        logger.info('Downloading: {} from {}'.format(scene_id, download_url))
        wget.download(download_url, temp_file)
        print()
        logger.info('Extracting to {}'.format(path))
        tar = tarfile.open(temp_file)
        tar.extractall(path=path)
        tar.close()
        logger.info('Removing {}'.format(temp_file))
        os.remove(temp_file)
        logger.info('{} complete'.format(scene_id))


    # get pipped in response
    resp = ""
    for line in sys.stdin:
        resp += line

    # convert string into dict
    resp = json.loads(resp)
    nfiles = resp['data']['numberReturned']
    logger.info("Number of files: {}".format(nfiles))
    results = resp['data']['results']

    for i, result in enumerate(results):
        # Run Downloads as threads so they keyboard interupts are
        # deferred until download is complete

        logger.info('{}/{}'.format(i, nfiles))
        job = Thread(target=download_from_result, args=(result, root))
        job.start()
        job.join()


@click.command()
@click.argument("dataset")
@click.argument("root")
@click.argument("db")
@click.option("--host", default="localhost", help="host id")
@click.option("--port", default="5432", help="port number")
@click.option("--user", help="username for database")
@click.option("--password", help="password for database")
def to_sql(db, dataset, root, host, port, user, password):
    """
    Upload the dataset to a database
    """
    if not dataset in sql.func_map.keys():
        logger.error("{} is not a valid dataset".format(dataset))

    dataset_root = os.path.join(root, dataset)

    # The dirs with important files will always be in leaf nodes
    leaf_dirs = list()
    for root, dirs, files in os.walk(dataset_root):
        if files and [f for f in files if not f.startswith('.')]:
            leaf_dirs.append(root)

    logger.info("{} Folders found".format(len(leaf_dirs)))

    # only suppoort postgres for now
    engine = create_engine('postgresql://{}:{}@{}:{}/{}'.format(user,password,host,port,db))
    for dir in leaf_dirs:
        logger.info("Uploading {}".format(dir))
        try:
            func_map[dataset](dir, engine)
        except Exception as e:
            logger.error("ERROR: {}".format(e))
            import traceback
            traceback.print_exc()

@click.command(context_settings=dict(
    ignore_unknown_options=True,
    allow_extra_args=True,
))
@click.argument("input", required=True)
@click.argument("bayleef_data", required=True)
@click.option("--add-option", "-ao", default='', help="Text containing misc. sbatch parameters")
@click.option("--log", "-l", default=None, help="Log output directory, default is redirected to /dev/null")
@click.option("--cpus", "-c", default=1, help="CPUs per job. Default = 1")
@click.option("--mem", '-m', default=4, help="Memory per job in gigabytes. Default = 4")
@click.option("--time", "-t", default='01:00:00', help="Max time per job, default = one hour.")
@click.option("--njobs", "-n", default='-1', help="Max number of conccurent jobs, -1 for unlimited. Default = -1")
def sbatch_master(input, bayleef_data, add_option, njobs, **options):
    """
    Run load-master command as sbatch jobs. Strongly reccomended that this is run directly on
    the slurm master.
    """

    if not os.path.exists(options['log']):
        raise Exception('Log directory {} is not a directory or does not exist'.format(options['log']))

    if not os.path.exists(bayleef_data):
        raise Exception('Bayleef data directory {} is not a directory or does not exist'.format(bayleef_data))

    glob_pattern = re.compile(fnmatch.translate(input+'/**/*.hdf'), re.IGNORECASE).pattern
    files = glob(glob_pattern, recursive=True)

    logger.info("sbatch options: log={log} cpus={cpus} mem={mem} time={time} njobs={njobs}".format(**options, njobs=njobs))
    logger.info("other options: {}".format(add_option if add_option else None))

    for file in files:
        command = "bayleef load-master {} {}".format(file, bayleef_data)
        logger.info("Dispatching {}".format(command))
        out = sbatch(wrap=command, **options)
        logger.info(out)
        limit_jobs(limit=njobs)

@click.command()
@click.argument("input", required=True)
@click.argument("bayleef_data", required=True)
@click.option("-r", default=False, help="Set to recursively glob .HDF files (Warning: Every .HDF file under the directory will be treated as a Master file)")
def load_master(input, bayleef_data, r):
    """
    Load master data.

    parameters
    ----------

    in : str
         root directory containing master files, .HDFs are recursively globbed.

    bayleef_data : str
                   root of the bayleef data directory
    """

    files = input
    if not r: # if not recursive
        files = [input]
    else:
        glob_pattern = re.compile(fnmatch.translate(input+'/**/*.hdf'), re.IGNORECASE).pattern
        files = glob(glob_pattern, recursive=True)

    total = len(files)

    logger.info("{} Files Found".format(total))
    for i, file in enumerate(files):
        logger.info('{}/{} ({}) - Proccessing {}'.format(i, total, round(i/total, 2), file))
        ingest.master(bayleef_data, file)

bayleef.add_command(to_sql, "to-sql")
bayleef.add_command(login)
bayleef.add_command(logout)
bayleef.add_command(datasets)
bayleef.add_command(dataset_fields, "dataset-fields")
bayleef.add_command(metadata)
bayleef.add_command(search)
bayleef.add_command(download_options, "download-options")
bayleef.add_command(download_url, "download-url")
bayleef.add_command(batch_download, "download")
bayleef.add_command(load_master, "load-master")
bayleef.add_command(sbatch_master, "sbatch-master")
