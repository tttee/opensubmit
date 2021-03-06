SHELL = /bin/bash

.PHONY: build docs

build:
	# Build the install packages.
	pip install -r requirements.txt

	pushd web; ./setup.py bdist_wheel; popd
	mv web/dist/* dist/
	rmdir web/dist
	rm -rf ./web/build
	rm -rf ./web/*.egg-info/

	pushd executor; ./setup.py bdist_wheel; popd
	mv executor/dist/* dist/
	rmdir executor/dist
	rm -rf ./executor/build
	rm -rf ./executor/*.egg-info/

docs:
	pushd docs; make html; popd

venv:
	# Create a virtualenv.
	# Activate it afterwards with "source venv/bin/activate"
	(python3.4 -m venv venv; \
	 source venv/bin/activate; \
	 pip install -r requirements.txt; \
	 pushd executor; pip install -r requirements.txt; popd; \
	 pushd web; pip install -r requirements.txt; popd;)

uninstall:
	pip uninstall -y opensubmit-web
	pip uninstall -y opensubmit-exec

re-install: build uninstall
	# Installs built packages locally.
	# This is intended for staging tests in a virtualenv.
	# On production systems, install a release directly from PyPI.
	pip install --upgrade dist/*.whl

docker: build
	# Create Docker image, based on fresh build
	pushd dist; docker build .; popd

tests:
	# Run all tests.
	pushd web; ./manage.py test; popd

coverage:
	# Run all tests and obtain coverage information.
	coverage run ./web/manage.py test opensubmit.tests; coverage html

clean:
	rm -f  ./dist/*.whl
	rm -f  ./.coverage
	rm -rf ./htmlcov
	find . -name "*.bak" -delete

clean-docs:
	rm -rf docs/formats

pypi: build
	# Upload built packages to PyPI.
	# Assumes valid credentials in ~/.pypirc
	twine upload dist/opensubmit_*.whl
