import attmap
import os
from collections import Iterable
import oyaml as yaml
import logging
import errno
import time
import sys
from ubiquerg import mkabs


_LOGGER = logging.getLogger(__name__)


FILEPATH_KEY = "_file_path"
RO_KEY = "_ro"
LOCK_PREFIX = "lock."


### Hack for string indexes of both ordered and unordered yaml representations
# Credit: Anthon
# https://stackoverflow.com/questions/50045617
# https://stackoverflow.com/questions/5121931
# The idea is: if you have yaml keys that can be interpreted as an int or a float,
# then the yaml loader will convert them into an int or a float, and you would
# need to access them with dict[2] instead of dict['2']. But since we always
# expect the keys to be strings, this doesn't work. So, here we are adjusting
# the loader to keep everything as a string. This happens in 2 ways, so that
# it's compatible with both yaml and oyaml, which is the orderedDict version.
# this will go away in python 3.7, because the dict representations will be
# ordered by default.
def my_construct_mapping(self, node, deep=False):
    data = self.construct_mapping_org(node, deep)
    return {(str(key) if isinstance(key, float) or isinstance(key, int) else key): data[key] for key in data}

def my_construct_pairs(self, node, deep=False):
    # if not isinstance(node, MappingNode):
    #     raise ConstructorError(None, None,
    #             "expected a mapping node, but found %s" % node.id,
    #             node.start_mark)
    pairs = []
    for key_node, value_node in node.value:
        key = str(self.construct_object(key_node, deep=deep))
        value = self.construct_object(value_node, deep=deep)
        pairs.append((key, value))
    return pairs

yaml.SafeLoader.construct_mapping_org = yaml.SafeLoader.construct_mapping
yaml.SafeLoader.construct_mapping = my_construct_mapping
yaml.SafeLoader.construct_pairs = my_construct_pairs
### End hack


class YacAttMap(attmap.PathExAttMap):
    """
    A class that extends AttMap to provide yaml reading and writing.

    The YacAttMap class is a YAML Configuration Attribute Map. Think of it as a
    python representation of your YAML configuration file, that can do a lot of
    cool stuff. You can access the hierarchical YAML attributes with dot
    notation or dict notation. You can read and write YAML config files with
    easy functions. It also retains memory of the its source filepath. If both a
    filepath and an entries dict are provided, it will first load the file
    and then updated it with values from the dict.

    :param str | Iterable[(str, object)] | Mapping[str, object] entries: YAML
        filepath or collection of key-value pairs.
    :param str filepath: YAML filepath to the config file.
    """

    def __init__(self, entries=None, filepath=None, yamldata=None, ro=False, wait_max=100):

        if isinstance(entries, str):
            # If user provides a string, it's probably a filename we should read
            # This should be removed at a major version release now that the
            # filepath argument exists, but we retain it for backwards
            # compatibility
            _LOGGER.debug("The entries argument should be a dict. If you want to read a file, use the filepath arg")
            filepath = entries
            entries = None

        if filepath:
            setattr(self, RO_KEY, ro)
            # If user provides a string, it's probably a filename we should read
            # Check if user intends to update the file
            if not getattr(self, RO_KEY):
                # attempt to lock the file
                lock_path = _make_lock_path(filepath)
                if os.path.exists(lock_path):
                    raise OSError("The file is locked: {}".format(filepath))
                else:
                    try:
                        _create_file_racefree(lock_path)
                    except OSError as e:
                        if e.errno == errno.EEXIST:
                            # Rare case: file already exists;
                            # the lock has been created in the split second since the last lock existence check,
                            # wait for the lock file to be gone, but no longer than `wait_max`.
                            print("Could not create a lock file, it already exists: {}".format(lock_path))
                            _wait_for_lock(lock_path, wait_max)
            file_contents = load_yaml(filepath)
            if entries:
                file_contents.update(entries)

            entries = file_contents

        if yamldata:
            entries = yaml.load(yamldata, yaml.SafeLoader)

        super(YacAttMap, self).__init__(entries or {})
        if filepath:
            setattr(self, FILEPATH_KEY, mkabs(filepath))

    def write(self, filename=None):
        """
        Writes the contents to a file.

        Makes sure that the object has been created with write capabilities

        :param str filename: a file path to write to
        :raise OSError: when the object has been created in a read only mode or other process has locked the file
        :return str: the path to the created file
        """
        if hasattr(self, RO_KEY) and getattr(self, RO_KEY):
            raise OSError("You can't write to a file that was read in RO mode.")
        filename = filename or getattr(self, FILEPATH_KEY)
        if not filename:
            raise Exception("No filename provided.")
        lock = _make_lock_path(filename)
        if not hasattr(self, RO_KEY) and os.path.exists(lock):
            # if the object hasn't been created by reading from file, but a lock exists
            raise OSError("You can't write to a file that was locked by a different process.")
        with open(filename, 'w') as f:
            f.write(self.to_yaml())
        if os.path.exists(lock):
            # remove the lock after writing
            os.remove(lock)
        return os.path.abspath(filename)

    @property
    def _lower_type_bound(self):
        """ Most specific type to which an inserted value may be converted """
        return YacAttMap

    def __repr__(self):
        # Here we want to render the data in a nice way; and we want to indicate
        # the class if it's NOT a YacAttMap. If it is a YacAttMap we just want
        # to give you the data without the class name.
        return self._render(self._simplify_keyvalue(self._data_for_repr(), self._new_empty_basic_map),
                            exclude_class_list="YacAttMap")

    def _excl_from_repr(self, k, cls):
        return k in (FILEPATH_KEY, RO_KEY)


def _wait_for_lock(lock_file, wait_max):
    """
    Just sleep until the lock_file does not exist or a lock_file-related dynamic recovery flag is spotted

    :param str lock_file: Lock file to wait upon.
    """
    sleeptime = .5
    first_message_flag = False
    dot_count = 0
    totaltime = 0
    while os.path.isfile(lock_file):
        if first_message_flag is False:
            print("Waiting for file lock: " + lock_file)
            first_message_flag = True
        else:
            sys.stdout.write(".")
            dot_count = dot_count + 1
            if dot_count % 60 == 0:
                sys.stdout.write("")
        time.sleep(sleeptime)
        totaltime += sleeptime
        sleeptime = min(sleeptime + 2.5, 20)
        if totaltime >= wait_max:
            raise RuntimeError("The maximum wait time has been reached")

    if first_message_flag:
        print("File unlocked")


def _create_file_racefree(file):
    """
    Creates a file, but fails if the file already exists.

    This function will thus only succeed if this process actually creates the file;
    if the file already exists, it will cause an OSError, solving race conditions.

    :param str file: File to create.
    """
    write_lock_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(file, write_lock_flags)
    os.close(fd)
    return file


def _make_lock_path(lock_name_base):
    """
    Create path to lock file with given name as base.

    :param str lock_name_base: Lock file name
    :return str: Path to the lock file.
    """
    base, name = os.path.split(lock_name_base)
    lock_name = name if name.startswith(LOCK_PREFIX) else LOCK_PREFIX + name
    return lock_name if not base else os.path.join(base, lock_name)


def load_yaml(filename):
    with open(filename, 'r') as f:
        data = yaml.safe_load(f)
    return data


def get_first_env_var(ev):
    """
    Get the name and value of the first set environment variable

    :param str | Iterable[str] ev: a list of the environment variable names
    :return (str, str): name and the value of the environment variable
    """
    if isinstance(ev, str):
        ev = [ev]
    elif not isinstance(ev, Iterable):
        raise TypeError("Env var must be single name or collection of names; "
                        "got {}".format(type(ev)))
    # TODO: we should handle the null (not found) case, as client code is inclined to unpack, and ValueError guard is vague.
    for v in ev:
        try:
            return v, os.environ[v]
        except KeyError:
            pass


def select_config(config_filepath=None,
                  config_env_vars=None,
                  default_config_filepath=None,
                  check_exist=True,
                  on_missing=lambda fp: IOError(fp)):
    """
    Selects the config file to load.

    This uses a priority ordering to first choose a config filepath if it's given,
    but if not, then look in a priority list of environment variables and choose
    the first available filepath to return.

    :param str | NoneType config_filepath: direct filepath specification
    :param Iterable[str] | NoneType config_env_vars: names of environment
        variables to try for config filepaths
    :param str default_config_filepath: default value if no other alternative
        resolution succeeds
    :param bool check_exist: whether to check for path existence as file
    :param function(str) -> object on_missing: what to do with a filepath if it
        doesn't exist
    """

    # First priority: given file
    if config_filepath:
        if not check_exist or os.path.isfile(config_filepath):
            return config_filepath
        _LOGGER.error("Config file path isn't a file: {}".
                      format(config_filepath))
        result = on_missing(config_filepath)
        if isinstance(result, Exception):
            raise result
        return result

    _LOGGER.debug("No local config file was provided")
    selected_filepath = None

    # Second priority: environment variables (in order)
    if config_env_vars:
        _LOGGER.debug("Checking for environment variable: {}".format(config_env_vars))

        cfg_env_var, cfg_file = get_first_env_var(config_env_vars) or ["", ""]

        if not check_exist or os.path.isfile(cfg_file):
            _LOGGER.debug("Found config file in {}: {}".
                          format(cfg_env_var, cfg_file))
            selected_filepath = cfg_file
        else:
            _LOGGER.info("Using default config. No config found in env "
                         "var: {}".format(str(config_env_vars)))
            selected_filepath = default_config_filepath
    else:
        _LOGGER.error("No configuration file found.")

    return selected_filepath