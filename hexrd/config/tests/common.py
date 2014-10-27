import os
import tempfile

from hexrd import config
from hexrd import testing


test_data = {
    'existing_path': os.path.abspath('..'),
    'nonexistent_path': 'an_unlikely_name_for_a_directory',
    'existing_file': __file__,
    'nonexistent_file': 'an_unlikely_name_for_a_file.dat',
    }



class YmlTestCase(testing.TestCase):


    file_name = None


    @classmethod
    def setUpClass(cls):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            cls.file_name = f.name
            f.file.write(cls.get_reference_data())


    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.file_name):
            os.remove(cls.file_name)


    def setUp(self):
        self.cfgs = config.open(self.file_name)
        self.cfg = self.cfgs[0]


    def tearDown(self):
        del(self.cfgs)
        del(self.cfg)


    @classmethod
    def get_reference_data(cls):
        raise NotImplementedError
