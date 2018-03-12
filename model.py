#!/usr/local/bin/python3

import tensorflow as tf
import numpy as np
from tensorflow.python import debug as tf_debug

# TODO: Enable reuse of graph while loading weights etc. 

#### Sub-modules used by network blocks ####

def conv1d(inputs,
           filters,
           kernel_size,
           strides=1,
           padding='same',
           data_format='channels_last',
           dilation_rate=1,
           activation=None,
           use_bias=True,
           kernel_initializer=None,
           bias_initializer=tf.zeros_initializer(),
           kernel_regularizer=None,
           bias_regularizer=None,
           activity_regularizer=None,
           kernel_constraint=None,
           bias_constraint=None,
           trainable=True,
           name=None,
           reuse=None):
    """
    A wrapper of tf.layers.conv1d that additionally accepts padding='causal' (causal convolutions) 
    For kernel size - k_size, causal convolutions are implemented by padding k_size-1
    zeros to the left of the input, and computing the output without any padding. 
    """

    if padding=='causal':
        # zero pad left side of input
        n_pads = dilation_rate*(kernel_size-1)
        inputs = tf.pad(inputs,[[0,0],[n_pads,0],[0,0]]) 
        padding = 'valid'

    params = {"inputs":inputs, "filters":filters, "kernel_size":kernel_size,
              "strides":strides,"padding":padding,"data_format":data_format,
              "dilation_rate":dilation_rate,"activation":activation,"use_bias":use_bias,
              "kernel_initializer":kernel_initializer,"bias_initializer":bias_initializer,
              "kernel_regularizer":kernel_regularizer,"bias_regularizer":bias_regularizer,
              "activity_regularizer":activity_regularizer,"kernel_constraint":kernel_constraint,
              "bias_constraint":bias_constraint,"trainable":trainable,"name":name,"reuse":reuse}   

    conv_out = tf.layers.conv1d(**params)

    return conv_out

# TODO: Clean up the arguments passed with *args or *kwargs
def highway_activation_conv(X,kernel_size,
                            dilation_rate=1,
                            padding='same',
                            scope='highwayConv'):
    """
    Implements Highway Convolution layer as in Tachibana et. al (2017) 
    HC^{d<-d}_{k_size*dil_rate} = Highway(X;C^{2d<-d}_{k_size*dil_rate})

    Here [H1, H2] = C^{2d<-d}_{k_size*dil_rate} (Convolution with kernel k_size, dilation dil_rate, and 
                                        channels in: d, out: 2d; split into blocks)
    Uses zero-padding for convolution to keep output length the same.                                         

    Highway(X;H1,H2) = sigmoid(H1)*H2 + (1-sigmoid(H1))*X 
                                      (varies from X to H2 with H1 from 0 to 1)                                       

    Args:
        X (tf.tensor): Input tensor (shape: batch_size, N, d)
        k_size (int): kernel size
        dil_rate (int): dilation factor
        scope (str): variable scope

    Returns:
        HC (tf.tensor): Output activation tensor (shape: batch_size, N, d)
    """
    d = X.shape.as_list()[2]

    with tf.variable_scope(scope):
        params = {"inputs":X, "filters":2*d, "kernel_size":kernel_size,
                  "dilation_rate":dilation_rate, "padding":padding}
        X_conv = conv1d(**params) # (batch_size, N, 2d)
        H1, H2 = X_conv[:,:,:d], X_conv[:,:,d:] # splitting into blocks
        HC = tf.multiply(tf.nn.sigmoid(H1),H2) + tf.multiply(1-tf.nn.sigmoid(H1),X)
    return HC

# TODO: Add a computation/check of effective field of resolution
def hc_dilation_block(X,local_scope,num_layers=4,kernel_size=3,dilation_rate=3,padding='same'):
    """
    Implements a stack of increasingly dilating highway conv layers. 
    e.g. For num_layers=4, k=3, dil=3 we get: HC(3,27) <- HC(3,9) <- HC(3,3) <- HC(3,1)

    Args:
        X (tf.tensor): Input tensor (shape: batch_size, length, channels)
        local_scope (str): variable scope
        num_layers (int): Number of stacked highway conv blocks 
        kernel_size (int): each highway conv kernel size
        dilation_rate (int): factor to exponentially increase dilation_rate, starting from 1
        padding (str): 'same' or 'causal'

    Returns:
        L (tf.tensor): Output tensor (shape: batch_size, length, channels)
    """
    L = X
    with tf.variable_scope(local_scope):
        for i in range(num_layers): 
            L = highway_activation_conv(L,kernel_size=kernel_size,dilation_rate=dilation_rate**i,
                                        padding=padding,scope="HC"+str(i))
    return L


###### Main network blocks #######


def TextEncBlock(L,d,scope="TextEncBlock"):
    """
    Implements the TextEnc module from Tachibana et. al (2017)

    Args:
        L (tf.tensor): Input tensor from text sequence embedding (shape: batch_size, N, e)
        d (int): Dimension of output matrices V,K (shape: batch_size, N, d)
        scope (str): variable scope

    Returns:
        KV (tf.tensor): Concatenated tensor of blocks V,K (shape: batch_size, N, 2*d)
    """
    e = L.shape.as_list()[2]

    with tf.variable_scope(scope):
        conv_params = {"filters":2*d,"kernel_size":1,"padding":"same"} 
        with tf.variable_scope("C_block1"):
            L1 = tf.nn.relu(conv1d(inputs=L,**conv_params)) # relu(conv)
            L2 = conv1d(inputs=L1,**conv_params) # conv
        # hc_dilation_blocks 
        L3_HC1_1 = hc_dilation_block(L2,local_scope="HC_dilation_block1")
        L3_HC1_2 = hc_dilation_block(L3_HC1_1,local_scope="HC_dilation_block2")
        # highway_conv_block2
        with tf.variable_scope("HC_block2"):
            L4_HC2_1 = highway_activation_conv(L3_HC1_2,kernel_size=3,scope="HC1")
            L4_HC2_2 = highway_activation_conv(L4_HC2_1,kernel_size=3,scope="HC2")
        # highway_conv_block3
        with tf.variable_scope("HC_block3"):
            L5_HC3_1 = highway_activation_conv(L4_HC2_2,kernel_size=1,scope="HC1")
            KV = highway_activation_conv(L5_HC3_1,kernel_size=1,scope="HC2")

    return KV 


def AudioEncBlock(S,d,scope="AudioEncBlock"):
    """
    Implements the AudioEncBlock from Tachibana et. al (2017)
    This block encodes the feedback input S (shape FxT) to the decoder into
    internal encodings used to compute attention scores Q (shape dxT). 

    During training, the feedback input S is T target mel frames from one previous
    time-step ( zero + first(T-1) frames ). During inference, S starts from 0 and is 
    appended to as output frames are produced by AudioDecBlock. 
    All conv and highway conv blocks used are causal. 

    Args:
        S (tf.tensor): Feedback tensor of mel frames (shape: batch_size, T, F)
        d (int): Dimension of internal state (has to match that of K for attention)
        scope (str): variable scope

    Returns:
        Q (tf.tensor): Encoded tensor (shape: batch_size, T, d)
    """
    F = S.shape.as_list()[2]

    with tf.variable_scope(scope):
        conv_params = {"filters":d,"kernel_size":1,"padding":'causal'} 
        with tf.variable_scope("C_block"):
            L1 = tf.nn.relu(conv1d(S,**conv_params)) # relu(conv)
            L2 = tf.nn.relu(conv1d(L1,**conv_params)) # relu(conv)
            L3 = conv1d(L2,**conv_params) # conv (shape: batch_size, T, d)
        # hc_dilation_blocks 
        L4_HCD1 = hc_dilation_block(L3,local_scope='HC_dilation_block1',padding='causal')
        L4_HCD2 = hc_dilation_block(L4_HCD1,local_scope='HC_dilation_block2',padding='causal')
        # highway conv block
        with tf.variable_scope("HC_block"):
            L5_HC1 = highway_activation_conv(L4_HCD2,kernel_size=3,dilation_rate=3,
                                             padding='causal',scope='HC1')
            Q = highway_activation_conv(L5_HC1,kernel_size=3,dilation_rate=3,
                                             padding='causal',scope='HC2')           

    return Q


def AudioDecBlock(RQ,F,scope='AudioDecBlock'):
    """
    Implements the AudioDecBlock from Tachibana et. al (2017)
    This block decodes concatenated internal encodings - R (dxT) (attention output from TextEnc)
    and Q (dxT) (encoding of feedback inputs S) - into mel frames one step ahead Yhat (FxT).
    All conv and highway conv blocks used are causal. Final layer outputs are produced by an
    element-wise sigmoid layer over F outputs. 

    Args:
        RQ (tf.tensor): Concatenated tensor of R,Q (shape: batch_size, T, 2d)
        F (int): Number of mel frames 
        scope (str): variable scope

    Returns:
        Ylogit (tf.tensor): Output tensor of logit (before sigmoid) (shape: batch_size, T, F)
        Yhat (tf.tensor): Output tensor of mel frames (shape: batch_size, T, F)
    """   
    d = RQ.shape.as_list()[2]//2

    with tf.variable_scope(scope):
        conv_params = {"filters":d,"kernel_size":1,"dilation_rate":1,"padding":'causal'} 
        with tf.variable_scope('C_layer1'): # conv layer 
            L1 = conv1d(RQ,**conv_params)
        # hc_dilation_block
        L2 = hc_dilation_block(L1,local_scope='HC_dilation_block',padding='causal')
        with tf.variable_scope('HC_block'): # highway conv block
            L3 = highway_activation_conv(L2,kernel_size=3,dilation_rate=1,
                                         padding='causal',scope='HC1')
            L4 = highway_activation_conv(L3,kernel_size=3,dilation_rate=1,
                                         padding='causal',scope='HC2')           
        with tf.variable_scope('C_layer2'): # relu(conv) block
            L5 = L4
            for _ in range(3):
                L5 = tf.nn.relu(conv1d(L5,**conv_params))
            conv_params["filters"] = F
            Ylogit = conv1d(L5,**conv_params)
            Yhat = tf.nn.sigmoid(Ylogit) # sigmoid(conv) output layer

    return Ylogit, Yhat


def AttentionBlock(KV,Q,scope='AttentionBlock'):
    """
    Implements a scaled key-value dot product attention mechanism as in Tachibana et. al (2017)

    Args:
        KV (tf.tensor): Concatenated tensor of K,V (shape: batch_size, N, 2*d)
        Q (tf.tensor): Encoding of feedback input from AudioEnd block (shape: batch_size, T, d)

    Returns:
        A (tf.tensor): shape TxN tensor of allignment between K and Q
        R (tf.tensor): output from V based on attention (shape: batch_size, T, d)
    """

    d = Q.shape.as_list()[2]
    K,V = KV[:,:,:d], KV[:,:,d:] # splitting out into blocks

    # TODO: Add guided attention loss computation
    A = tf.nn.softmax(tf.matmul(Q,
                      tf.transpose(K,[0,2,1]))/tf.sqrt(tf.cast(d,tf.float32))) # scaled d.p. attention
    R = tf.matmul(A,V)

    return A, R


###### Test functions ######

def test_modules(mode,**kwargs):

    # test tensor shape to be fed in
    X_len = 4 
    X =  tf.placeholder(dtype=tf.float32,shape=(None,X_len,5))
    # test tensor values used for test
    X_feed = np.zeros([1,X_len,5])
    X_feed[:,1,:] = 1

    if mode=='conv':

        filters, k_size, dil_rate = 5, 3, 3
        padding = 'same' if 'padding' not in kwargs else kwargs['padding']
        kernel_val = np.tile([3,0,5],(5,5,1)).transpose() # shape (3,5,5)
        kernel_init = tf.constant_initializer(kernel_val)
        params = {"inputs":X, "filters":filters, "kernel_size":k_size,
                  "dilation_rate":dil_rate, "padding":padding,
                  "kernel_initializer":kernel_init}   
        print("Testing conv layer with k_size:{}, dilation: {}, padding: {}, on input shape:".format(
            k_size,dil_rate,padding),X.shape.as_list())                 
        out_tensor = conv1d(**params)

    elif mode=='highway_conv':

        k_size, dil_rate = 3,3
        padding = 'same' if 'padding' not in kwargs else kwargs['padding']
        print("Testing highway conv layer with k_size:{}, dilation: {}, padding: {}, on input shape:".format(
        k_size,dil_rate,padding),X.shape.as_list())       
        out_tensor = highway_activation_conv(X,k_size,dil_rate,padding,mode+padding)

    elif mode=='text_enc_block':

        d=2
        print("Testing TextEncBlock with d:{}, on input shape:".format(d),
            X.shape.as_list())       
        out_tensor = TextEncBlock(X,d)

    elif mode=='audio_enc_block':

        d=2
        print("Testing AudioEncBlock with d:{}, on input shape:".format(d),
            X.shape.as_list())       
        out_tensor = AudioEncBlock(X,d)       

    elif mode=='audio_dec_block':

        F=4
        print("Testing AudioDecBlock with F:{}, on input shape:".format(F),
            X.shape.as_list())       
        _ , out_tensor = AudioDecBlock(X,F)              

    elif mode=='attention_block':

        d=2
        print("Testing AttentionBlock with d:{}, on input shape:".format(d),
            X.shape.as_list())       
        KV = TextEncBlock(X,d)
        Q = AudioEncBlock(X,d)       
        out_tensor, _ = AttentionBlock(KV,Q)                     

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        out_np = sess.run(out_tensor,{X:X_feed})
    print("Output (shape: {}) generated:".format(out_np.shape))
    np.set_printoptions(precision=3)
    print(out_np)       


if __name__=="__main__":

    # test_modules('conv')
    test_modules('conv',padding='causal')
    # test_modules('highway_conv')
    # test_modules('highway_conv',padding='causal')
    # test_modules('text_enc_block')
    # test_modules('audio_enc_block')
    test_modules('audio_dec_block')
    test_modules('attention_block')
