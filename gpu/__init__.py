"""GPU dispatch layer for the Ranomics tools hub.

Wave-0 contract owner (Stream A). Other streams import ``ModalClient`` from
``gpu.modal_client`` to submit jobs against Kendrew's Modal apps. The
implementation is a stub at the moment — it returns a fake FunctionCall id
so downstream streams can wire their forms and fixtures while Stream E
finalises the real Modal integration.
"""
