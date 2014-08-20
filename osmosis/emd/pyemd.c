#include <Python.h>
#include "emd.h"

// define PyInt_* macros for Python 3.x
#ifndef PyInt_Check
#define PyInt_Check             PyLong_Check
#define PyInt_FromLong          PyLong_FromLong
#define PyInt_AsLong            PyLong_AsLong
#define PyInt_Type              PyLong_Type
#endif


static PyObject *compute_emd(PyObject *self, PyObject *args, PyObject *keywds)
{
  static char *kwlist[] = {"weight1", "weight2", "cost", NULL};

  PyObject *weight1, *weight2, *cost;
  float *w1, *w2, *c;
  int length1, length2;
  signature_t signature1, signature2;
  float distance;
  PyObject *item;
  int i;

  if(!PyArg_ParseTupleAndKeywords(args, keywds, "OOO", kwlist,
				  &weight1, &weight2, &cost))
     return NULL;

  if(!PySequence_Check(weight1)) {
    PyErr_SetString(PyExc_TypeError, "weight1 must be a sequence");
    return NULL;
  }

  if(!PySequence_Check(weight2)) {
    PyErr_SetString(PyExc_TypeError, "weight2 must be a sequence");
    return NULL;
  }

  length1 = PySequence_Size(weight1);
  length2 = PySequence_Size(weight2);

  if(PySequence_Size(weight1) != length1) {
    PyErr_SetString(PyExc_TypeError, "feature1 and weight1 must be the same");
    return NULL;
  }

  if(PySequence_Size(weight2) != length2) {
    PyErr_SetString(PyExc_TypeError, "feature2 and weight2 must be the same");
    return NULL;
  }

  w1 = alloca(length1 * sizeof(double));
  w2 = alloca(length2 * sizeof(double));
  c = alloca(length1 * length2 * sizeof(double));
  
  for(i = 0; i < length1; i ++) {
    item = PySequence_GetItem(weight1, i);
    if(!PyFloat_Check(item) && !PyInt_Check(item)) {
      Py_DECREF(item);
      PyErr_SetString(PyExc_TypeError, "w1 should be a sequence of numbers");
      return NULL;
    }
    w1[i] = PyFloat_AsDouble(item);
    Py_DECREF(item);
  }

  for(i = 0; i < length2; i ++) {
    item = PySequence_GetItem(weight2, i);
    if(!PyFloat_Check(item) && !PyInt_Check(item)) {
      Py_DECREF(item);
      PyErr_SetString(PyExc_TypeError, "w2 should be a sequence of numbers");
      return NULL;
    }
    w2[i] = PyFloat_AsDouble(item);
    Py_DECREF(item);
  }
  
  for(i = 0; i < (length1 * length2); i ++) {
    item = PySequence_GetItem(cost, i);
    if(!PyFloat_Check(item) && !PyInt_Check(item)) {
      Py_DECREF(item);
      PyErr_SetString(PyExc_TypeError, "cost should be a sequence of numbers");
      return NULL;
    }
    c[i] = PyFloat_AsDouble(item);
    Py_DECREF(item);
 }

  signature1.n = length1;
  //signature1.Features = f1;
  signature1.Weights = w1;

  signature2.n = length2;
  //signature2.Features = f2;
  signature2.Weights = w2;

  distance = emd(&signature1, &signature2, c, 0, 0);

  return Py_BuildValue("d", distance);
}

static PyMethodDef functions[] = {
    {"emd", (PyCFunction)compute_emd, METH_VARARGS | METH_KEYWORDS,
     "Compute the Earth Mover's Distance.\n\nParameters\n----------\nw1 : list (length n) \n\t The first set of weights \nw2 : list (length m)\n\tSecond set of weights\ndist : list (length n times m)\n\tAny distance metric between item i in w1 and item j in w2.\n"},
    {NULL, NULL, 0, NULL}
};

#if PY_MAJOR_VERSION >= 3
//-----------------------------------------------------------------------------
//   Declaration of module definition for Python 3.x.
//-----------------------------------------------------------------------------
static struct PyModuleDef g_ModuleDef = {
    PyModuleDef_HEAD_INIT,
    "emd",
    NULL,
    -1,
    functions,                             // methods
    NULL,                                  // m_reload
    NULL,                                  // traverse
    NULL,                                  // clear
    NULL                                   // free
};
#endif



#if PY_MAJOR_VERSION >= 3
PyObject * PyInit_emd(void)
{
	return PyModule_Create(&g_ModuleDef);
#else
PyMODINIT_FUNC initemd(void)
{
    Py_InitModule(
        "emd", functions
        );
#endif
}