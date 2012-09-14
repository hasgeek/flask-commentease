import os
from setuptools import setup, find_packages

here = os.path.abspath(os.path.dirname(__file__))
README = unicode(open(os.path.join(here, 'README.rst')).read(), 'utf-8')
CHANGES = unicode(open(os.path.join(here, 'CHANGES.rst')).read(), 'utf-8')

requires = [
    'Flask',
    'Flask-SQLAlchemy',
    'Flask-WTF',
    'coaster',
    'markdown',
    'bleach',
    ]

setup(name='Flask-Commentease',
      version='0.1',
      description='Comments and voting as a Flask blueprint',
      long_description=README,
      classifiers=[
        "Programming Language :: Python",
        "Programming Language :: Python :: 2.6",
        "Programming Language :: Python :: 2.7",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Development Status :: 3 - Alpha",
        "Topic :: Software Development :: Libraries",
        ],
      author='Kiran Jonnalagadda',
      author_email='kiran@hasgeek.com',
      url='https://github.com/hasgeek/flask-commentease',
      keywords='commentease',
      packages=find_packages(),
      include_package_data=True,
      zip_safe=True,
      test_suite='tests',
      install_requires=requires,
      dependency_links=[
          "https://github.com/hasgeek/coaster/tarball/master#egg=coaster",
          ]
      )
