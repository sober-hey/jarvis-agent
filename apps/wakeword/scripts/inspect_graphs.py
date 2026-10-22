import json
from pathlib import Path
import onnx
ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT/'models/original').glob('*.onnx'):
    m=onnx.load(path)
    print('\nMODEL', path.name)
    print('weights', [(t.name, list(t.dims)) for t in m.graph.initializer])
    for n in m.graph.node:
        attrs={a.name:onnx.helper.get_attribute_value(a) for a in n.attribute}
        if n.op_type=='Constant':
            attrs={k:onnx.numpy_helper.to_array(v).tolist() for k,v in attrs.items()}
        print(n.op_type, list(n.input), list(n.output),attrs if n.op_type!='If' else 'BRANCH')
