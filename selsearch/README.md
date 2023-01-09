```shell
# this compilation is not mandatory, because the python-only reimpl of selective search is used for producing selsearch masks. for using minimal tweaks of original opencv impl, this compilation is necessary. 
BINDIR=$(dirname $(which python))
OPENCVLIBDIR=$BINDIR/../lib
OPENCVINCLUDEDIR=$BINDIR/../include/opencv4
make OPENCVLIBDIR=$OPENCVLIBDIR OPENCVINCLUDEDIR=$OPENCVINCLUDEDIR
```
