import json
import re
import textwrap
import unittest

import pytest

from conan.cli.exit_codes import ERROR_INVALID_CONFIGURATION
from conans.client.graph.graph import BINARY_INVALID
from conans.test.assets.genconanfile import GenConanfile
from conans.util.files import save
from conans.test.utils.tools import TestClient, NO_SETTINGS_PACKAGE_ID


class TestValidate(unittest.TestCase):

    def test_validate_create(self):
        client = TestClient()
        conanfile = textwrap.dedent("""
            from conan import ConanFile
            from conan.errors import ConanInvalidConfiguration
            class Pkg(ConanFile):
                settings = "os"

                def validate(self):
                    if self.info.settings.os == "Windows":
                        raise ConanInvalidConfiguration("Windows not supported")
            """)

        client.save({"conanfile.py": conanfile})

        client.run("create . --name=pkg --version=0.1 -s os=Linux")
        self.assertIn("pkg/0.1: Package '9a4eb3c8701508aa9458b1a73d0633783ecc2270' created",
                      client.out)

        error = client.run("create . --name=pkg --version=0.1 -s os=Windows", assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)

        client.run("graph info --require pkg/0.1 -s os=Windows")
        self.assertIn("binary: Invalid", client.out)

        client.run("graph info --require pkg/0.1 -s os=Windows --format json")
        myjson = json.loads(client.stdout)
        self.assertEqual(myjson["nodes"][1]["binary"], BINARY_INVALID)

    def test_validate_header_only(self):
        client = TestClient()
        conanfile = textwrap.dedent("""
            from conan import ConanFile
            from conan.errors import ConanInvalidConfiguration
            from conan.tools.build import check_min_cppstd
            class Pkg(ConanFile):
                settings = "os", "compiler"
                options = {"shared": [True, False], "header_only": [True, False],}
                default_options = {"shared": False, "header_only": True}

                def package_id(self):
                   if self.info.options.header_only:
                       self.info.clear()

                def validate(self):
                    if self.info.options.get_safe("header_only") == "False":
                        if self.info.settings.get_safe("compiler.version") == "12":
                          raise ConanInvalidConfiguration("This package cannot exist in gcc 12")
                        check_min_cppstd(self, 11)
                        # These configurations are impossible
                        if self.info.settings.os != "Windows" and self.info.options.shared:
                            raise ConanInvalidConfiguration("shared is only supported under windows")

                    # HOW CAN WE VALIDATE CPPSTD > 11 WHEN HEADER ONLY?

            """)

        client.save({"conanfile.py": conanfile})

        client.run("create . --name pkg --version=0.1 -s os=Linux -s compiler=gcc "
                   "-s compiler.version=11 -s compiler.libcxx=libstdc++11")
        assert re.search(r"Package '(.*)' created", str(client.out))

        client.run("create . --name pkg --version=0.1 -o header_only=False -s os=Linux "
                   "-s compiler=gcc -s compiler.version=12 -s compiler.libcxx=libstdc++11",
                   assert_error=True)

        assert "Invalid: This package cannot exist in gcc 12" in client.out

        client.run("create . --name pkg --version=0.1  -o header_only=False -s os=Macos "
                   "-s compiler=gcc -s compiler.version=11 -s compiler.libcxx=libstdc++11 "
                   "-s compiler.cppstd=98",
                   assert_error=True)

        assert "Invalid: Current cppstd (98) is lower than the required C++ " \
               "standard (11)" in client.out

        client.run("create . --name pkg --version=0.1  -o header_only=False -o shared=True "
                   "-s os=Macos -s compiler=gcc "
                   "-s compiler.version=11 -s compiler.libcxx=libstdc++11 -s compiler.cppstd=11",
                   assert_error=True)

        assert "Invalid: shared is only supported under windows" in client.out

    def test_validate_compatible(self):
        client = TestClient()
        conanfile = textwrap.dedent("""
            from conan import ConanFile
            from conan.errors import ConanInvalidConfiguration
            class Pkg(ConanFile):
                settings = "os"

                def validate(self):
                    if self.info.settings.os == "Windows":
                        raise ConanInvalidConfiguration("Windows not supported")

                def compatibility(self):
                    if self.settings.os == "Windows":
                        return [{"settings": [("os", "Linux")]}]
            """)

        client.save({"conanfile.py": conanfile})

        client.run("create . --name=pkg --version=0.1 -s os=Linux")
        package_id = "9a4eb3c8701508aa9458b1a73d0633783ecc2270"
        missing_id = "ebec3dc6d7f6b907b3ada0c3d3cdc83613a2b715"
        self.assertIn(f"pkg/0.1: Package '{package_id}' created",
                      client.out)

        # This is the main difference, building from source for the specified conf, fails
        client.run("create . --name=pkg --version=0.1 -s os=Windows", assert_error=True)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)
        client.assert_listed_binary({"pkg/0.1": (missing_id, "Invalid")})

        client.run("install --requires=pkg/0.1@ -s os=Windows --build=pkg*", assert_error=True)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)
        self.assertIn("Windows not supported", client.out)

        client.run("install --requires=pkg/0.1@ -s os=Windows")
        self.assertIn(f"pkg/0.1: Main binary package '{missing_id}' "
                      f"missing. Using compatible package '{package_id}'",
                      client.out)
        client.assert_listed_binary({"pkg/0.1": (package_id, "Cache")})

        # --build=missing means "use existing binary if possible", and compatibles are valid binaries
        client.run("install --requires=pkg/0.1@ -s os=Windows --build=missing")
        self.assertIn(f"pkg/0.1: Main binary package '{missing_id}' "
                      f"missing. Using compatible package '{package_id}'",
                      client.out)
        client.assert_listed_binary({"pkg/0.1": (package_id, "Cache")})

        client.run("graph info --requires=pkg/0.1@ -s os=Windows")
        self.assertIn(f"pkg/0.1: Main binary package '{missing_id}' "
                      f"missing. Using compatible package '{package_id}'",
                      client.out)
        self.assertIn(f"package_id: {package_id}", client.out)

        client.run("graph info --requires=pkg/0.1@ -s os=Windows --build=pkg*")
        self.assertIn("binary: Invalid", client.out)

    def test_validate_remove_package_id_create(self):
        client = TestClient()
        conanfile = textwrap.dedent("""
               from conan import ConanFile
               from conan.errors import ConanInvalidConfiguration
               class Pkg(ConanFile):
                   settings = "os"

                   def validate(self):
                       if self.info.settings.os == "Windows":
                           raise ConanInvalidConfiguration("Windows not supported")

                   def package_id(self):
                       del self.info.settings.os
               """)

        client.save({"conanfile.py": conanfile})

        client.run("create . --name=pkg --version=0.1 -s os=Linux")
        self.assertIn("pkg/0.1: Package '{}' created".format(NO_SETTINGS_PACKAGE_ID), client.out)

        client.run("create . --name=pkg --version=0.1 -s os=Windows", assert_error=True)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)
        client.assert_listed_binary({"pkg/0.1": ("da39a3ee5e6b4b0d3255bfef95601890afd80709",
                                                 "Invalid")})

        client.run("graph info --requires=pkg/0.1@ -s os=Windows")
        self.assertIn("package_id: {}".format(NO_SETTINGS_PACKAGE_ID), client.out)

    def test_validate_compatible_also_invalid(self):
        client = TestClient()
        conanfile = textwrap.dedent("""
           from conan import ConanFile
           from conan.errors import ConanInvalidConfiguration
           class Pkg(ConanFile):
               settings = "os", "build_type"

               def validate(self):
                   if self.info.settings.os == "Windows":
                       raise ConanInvalidConfiguration("Windows not supported")

               def compatibility(self):
                   if self.settings.build_type == "Debug" and self.settings.os != "Windows":
                       return [{"settings": [("build_type", "Release")]}]
               """)

        client.save({"conanfile.py": conanfile})

        client.run("create . --name=pkg --version=0.1 -s os=Linux -s build_type=Release")
        package_id = "c26ded3c7aa4408e7271e458d65421000e000711"
        client.assert_listed_binary({"pkg/0.1": (package_id, "Build")})
        # compatible_packges fallback works
        client.run("install --requires=pkg/0.1@ -s os=Linux -s build_type=Debug")
        client.assert_listed_binary({"pkg/0.1": (package_id, "Cache")})

        error = client.run("create . --name=pkg --version=0.1 -s os=Windows -s build_type=Release",
                           assert_error=True)

        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)

        client.run("graph info --requires=pkg/0.1@ -s os=Windows")
        assert "binary: Invalid" in client.out

    def test_validate_compatible_also_invalid_fail(self):
        client = TestClient()
        conanfile = textwrap.dedent("""
           from conan import ConanFile
           from conan.errors import ConanInvalidConfiguration
           class Pkg(ConanFile):
               settings = "os", "build_type"

               def validate(self):
                   if self.info.settings.os == "Windows":
                       raise ConanInvalidConfiguration("Windows not supported")

               def compatibility(self):
                   if self.settings.build_type == "Debug":
                       return [{"settings": [("build_type", "Release")]}]
               """)

        client.save({"conanfile.py": conanfile})

        package_id = "c26ded3c7aa4408e7271e458d65421000e000711"
        client.run("create . --name=pkg --version=0.1 -s os=Linux -s build_type=Release")
        self.assertIn(f"pkg/0.1: Package '{package_id}' created",
                      client.out)
        # compatible_packges fallback works
        client.run("install --requires=pkg/0.1@ -s os=Linux -s build_type=Debug")
        client.assert_listed_binary({"pkg/0.1": (package_id, "Cache")})
        # Windows invalid configuration
        error = client.run("create . --name=pkg --version=0.1 -s os=Windows -s build_type=Release",
                           assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)

        error = client.run("install --requires=pkg/0.1@ -s os=Windows -s build_type=Release",
                           assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)

        # Windows missing binary: INVALID
        error = client.run("install --requires=pkg/0.1@ -s os=Windows -s build_type=Debug",
                           assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)

        error = client.run("create . --name=pkg --version=0.1 -s os=Windows -s build_type=Debug",
                           assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg/0.1: Invalid: Windows not supported", client.out)

        # info
        client.run("graph info --requires=pkg/0.1@ -s os=Windows")
        assert "binary: Invalid" in client.out
        client.run("graph info --requires=pkg/0.1@ -s os=Windows -s build_type=Debug")
        assert "binary: Invalid" in client.out

    @pytest.mark.xfail(reason="The way to check options of transitive deps has changed")
    def test_validate_options(self):
        # The dependency option doesn't affect pkg package_id, so it could find a valid binary
        # in the cache. So ConanErrorConfiguration will solve this issue.
        client = TestClient()
        client.save({"conanfile.py": GenConanfile().with_option("myoption", [1, 2, 3])
                                                   .with_default_option("myoption", 1)})
        client.run("create . --name=dep --version=0.1")
        client.run("create . --name=dep --version=0.1 -o dep/*:myoption=2")
        conanfile = textwrap.dedent("""
           from conan import ConanFile
           from conan.errors import ConanErrorConfiguration
           class Pkg(ConanFile):
               requires = "dep/0.1"

               def validate(self):
                   if self.options["dep"].myoption == 2:
                       raise ConanErrorConfiguration("Option 2 of 'dep' not supported")
           """)

        client.save({"conanfile.py": conanfile})
        client.run("create . --name=pkg1 --version=0.1 -o dep/*:myoption=1")

        client.save({"conanfile.py": GenConanfile().with_requires("dep/0.1")
                                                   .with_default_option("dep:myoption", 2)})
        client.run("create . --name=pkg2 --version=0.1")

        client.save({"conanfile.py": GenConanfile().with_requires("pkg1/0.1", "pkg2/0.1")})
        error = client.run("install .", assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg1/0.1: ConfigurationError: Option 2 of 'dep' not supported", client.out)

    @pytest.mark.xfail(reason="The way to check versions of transitive deps has changed")
    def test_validate_requires(self):
        client = TestClient()
        client.save({"conanfile.py": GenConanfile()})
        client.run("create . --name=dep --version=0.1")
        client.run("create . --name=dep --version=0.2")
        conanfile = textwrap.dedent("""
           from conan import ConanFile
           from conan.errors import ConanInvalidConfiguration
           class Pkg(ConanFile):
               requires = "dep/0.1"

               def validate(self):
                   # FIXME: This is a ugly interface DO NOT MAKE IT PUBLIC
                   # if self.info.requires["dep"].full_version ==
                   if self.requires["dep"].ref.version > "0.1":
                       raise ConanInvalidConfiguration("dep> 0.1 is not supported")
           """)

        client.save({"conanfile.py": conanfile})
        client.run("create . --name=pkg1 --version=0.1")

        client.save({"conanfile.py": GenConanfile().with_requires("pkg1/0.1", "dep/0.2")})
        error = client.run("install .", assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        self.assertIn("pkg1/0.1: Invalid: dep> 0.1 is not supported", client.out)

    def test_validate_package_id_mode(self):
        client = TestClient()
        save(client.cache.new_config_path, "core.package_id:default_unknown_mode=full_package_mode")
        conanfile = textwrap.dedent("""
          from conan import ConanFile
          from conan.errors import ConanInvalidConfiguration
          class Pkg(ConanFile):
              settings = "os"

              def validate(self):
                  if self.info.settings.os == "Windows":
                      raise ConanInvalidConfiguration("Windows not supported")
              """)
        client.save({"conanfile.py": conanfile})
        client.run("export . --name=dep --version=0.1")

        client.save({"conanfile.py": GenConanfile().with_requires("dep/0.1")})
        error = client.run("create . --name=pkg --version=0.1 -s os=Windows", assert_error=True)
        self.assertEqual(error, ERROR_INVALID_CONFIGURATION)
        client.assert_listed_binary({"dep/0.1": ("ebec3dc6d7f6b907b3ada0c3d3cdc83613a2b715",
                                                 "Invalid")})
        client.assert_listed_binary({"pkg/0.1": ("19ad5731bb09f24646c81060bd7730d6cb5b6108",
                                                 "Build")})
        self.assertIn("ERROR: There are invalid packages (packages that cannot "
                      "exist for this configuration):", client.out)
        self.assertIn("dep/0.1: Invalid: Windows not supported", client.out)

    def test_validate_export_pkg(self):
        # https://github.com/conan-io/conan/issues/9797
        c = TestClient()
        conanfile = textwrap.dedent("""
            from conan import ConanFile
            from conan.errors import ConanInvalidConfiguration

            class TestConan(ConanFile):
                def validate(self):
                    raise ConanInvalidConfiguration("never ever")
            """)
        c.save({"conanfile.py": conanfile})
        c.run("export-pkg . --name=test --version=1.0", assert_error=True)
        assert "Invalid: never ever" in c.out

    def test_validate_install(self):
        # https://github.com/conan-io/conan/issues/10602
        c = TestClient()
        conanfile = textwrap.dedent("""
            from conan import ConanFile
            from conan.errors import ConanInvalidConfiguration

            class TestConan(ConanFile):
                def validate(self):
                    raise ConanInvalidConfiguration("never ever")
            """)
        c.save({"conanfile.py": conanfile})
        c.run("install .", assert_error=True)
        assert "ERROR: conanfile.py: Invalid ID: Invalid: never ever" in c.out
