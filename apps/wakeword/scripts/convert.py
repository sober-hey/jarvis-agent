"""Faithful ONNX weight translation, numerical gate, then Core ML conversion."""
from pathlib import Path
import json
import numpy as np
import onnx
from onnx import numpy_helper
import torch
from torch import nn
from torch.nn import functional as F
import coremltools as ct
import onnxruntime as ort
ROOT=Path(__file__).resolve().parents[1]
torch.set_num_threads(1)

def weights(name):return {x.name:numpy_helper.to_array(x).copy() for x in onnx.load(ROOT/'models/original'/name).graph.initializer}
class Mel(nn.Module):
    def __init__(self):
        super().__init__();w=weights('melspectrogram.onnx')
        for key,name in [('real','0.stft.conv_real.weight'),('imag','0.stft.conv_imag.weight'),('mel','1.melW')]:self.register_buffer(key,torch.tensor(w[name]))
    def forward(self,x):
        real=F.conv1d(x[:,None,:],self.real,stride=160).transpose(1,2)
        imag=F.conv1d(x[:,None,:],self.imag,stride=160).transpose(1,2)
        y=(real.square()+imag.square())@self.mel
        y=10*torch.log(torch.clamp(y,min=1e-10))/2.3025851249694824
        y=torch.maximum(y,y.max()-80)
        return y/10+2
class Embed(nn.Module):
    def __init__(self):
        super().__init__();w=weights('embedding_model.onnx');self.layers=nn.ModuleList()
        for i in range(20):
            prefix='model/conv2d'+('' if i==0 else '_'+str(i))
            wk=prefix+('/Conv2D/ReadVariableOp:0' if i==19 else '/Conv2D_weights_fused_bn')
            kernel=w[wk];layer=nn.Conv2d(kernel.shape[1],kernel.shape[0],kernel.shape[2:],bias=i!=19,padding=(0,1) if i==0 or i%2==1 and i<19 else 0)
            layer.weight.data.copy_(torch.tensor(kernel))
            if i!=19:layer.bias.data.copy_(torch.tensor(w[prefix+'/Conv2D_bias_fused_bn']))
            self.layers.append(layer)
    def forward(self,x):
        x=x.reshape(-1,1,76,32)
        for i,layer in enumerate(self.layers):
            x=layer(x)
            if i!=19:x=torch.clamp(F.leaky_relu(x,.2),min=-0.4)
            if i in [2,6,10,14,18]:x=F.max_pool2d(x,(1,2) if i in [6,14] else (2,2))
        return x.reshape(-1,96)
class Head(nn.Module):
    def __init__(self):
        super().__init__();self.net=nn.Sequential(nn.Flatten(),nn.Linear(1536,128),nn.LayerNorm(128),nn.ReLU(),nn.Linear(128,128),nn.LayerNorm(128),nn.ReLU(),nn.Linear(128,1),nn.Sigmoid())
    def forward(self,x):return self.net(x)

def export(name,module,example,input_name,output_name):
    module.eval();trace=torch.jit.trace(module,example)
    model=ct.convert(trace,inputs=[ct.TensorType(name=input_name,shape=example.shape,dtype=np.float32)],outputs=[ct.TensorType(name=output_name,dtype=np.float32)],convert_to='mlprogram',minimum_deployment_target=ct.target.iOS16,compute_precision=ct.precision.FLOAT32,compute_units=ct.ComputeUnit.CPU_ONLY)
    model.short_description='Offline Jarvis wake-word '+name+'; openWakeWord-derived frontend; personal noncommercial use only.'
    model.author='JARVIS project; frontend weights dscripka/openWakeWord / Google speech_embedding'
    model.license='Frontend: see research/RESEARCH.md; personal noncommercial only pending upstream model licence clarification.'
    model.save(str(ROOT/'models'/f'{name}.mlpackage'))
    return model

def main():
    rng=np.random.default_rng(42)
    mel=Mel().eval();embed=Embed().eval()
    print('embedding clamp constant',weights('embedding_model.onnx')['const_fold_opt__287'])
    x=rng.normal(0,1000,(1,1760)).astype('float32');y=rng.uniform(0,10,(1,76,32,1)).astype('float32')
    rows=[]
    for name,module,xx,onnx_name,inn,outn in [('Melspectrogram',mel,x,'melspectrogram.onnx','audio','mel'),('SpeechEmbedding',embed,y,'embedding_model.onnx','mel','embedding')]:
        opt=ort.SessionOptions();opt.intra_op_num_threads=1
        s=ort.InferenceSession(str(ROOT/'models/original'/onnx_name),sess_options=opt)
        expected=s.run(None,{s.get_inputs()[0].name:xx})[0]
        if name=='Melspectrogram':expected=expected.reshape(1,8,32)/10+2
        else:expected=expected.reshape(1,96)
        with torch.no_grad():actual=module(torch.tensor(xx)).numpy()
        delta=float(abs(expected-actual).max());print(name,'torch parity',delta,flush=True)
        assert delta<.002,(name,delta)
        model=export(name,module,torch.tensor(xx),inn,outn)
        predicted=model.predict({inn:xx})[outn]
        cdelta=float(abs(predicted-expected).max());print(name,'coreml parity',cdelta,flush=True)
        assert cdelta<.01,(name,cdelta)
        rows.append({'model':name,'torch_max_error':delta,'coreml_max_error':cdelta})
    (ROOT/'evidence/conversion.json').write_text(json.dumps(rows,indent=2))
if __name__=='__main__':main()
